"""Frozen QA runtime inputs and trusted old-database setup."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from pydantic import ValidationError
from qa_fixtures import unit_manifest, unit_suite
from test_architect import valid_spec

from app.core.exceptions import BusinessException
from app.schemas.qa_suite import QARuntime, QASuite
from app.services.qa_assets import validate_manifest, validate_suite
from app.tools.qa_runner import qa_timeout_seconds


def trusted_runner():
    path = Path(__file__).resolve().parents[1] / "sandbox" / "acceptance.py"
    spec = importlib.util.spec_from_file_location("qa_runtime_test_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_suite():
    raw = unit_suite().model_dump(mode="json")
    raw["scenarios"][0]["runtime"] = {
        "env": {"APP_MODE": "production", "SEED_SAMPLE_DATA": "true"},
        "before_upgrade_revision": "0001_old",
        "seed_rows": [{"table": "legacy_records", "values": {"id": 1, "name": "old"}}],
    }
    return QASuite.model_validate(raw)


class QARuntimeTests(unittest.TestCase):
    def test_runtime_is_frozen_and_legacy_suite_hash_stays_stable(self):
        legacy = unit_suite()
        payload = legacy.model_dump(mode="json")
        for case in payload["scenarios"]:
            case.pop("runtime")
        expected = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(legacy.digest(), expected)

        suite = runtime_suite()
        validate_suite(suite, valid_spec(), None)
        changed = copy.deepcopy(suite.model_dump(mode="json"))
        changed["revision"] += 1
        changed["scenarios"][0]["runtime"]["env"]["APP_MODE"] = "development"
        with self.assertRaisesRegex(BusinessException, "不能删除场景"):
            validate_suite(QASuite.model_validate(changed), valid_spec(), None, suite)

    def test_runtime_rejects_controller_env_and_unsafe_fixture(self):
        for name in ("DATABASE_URL", "PYTHONPATH", "LD_PRELOAD", "FORGEAI_TOKEN", "PATH"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                QARuntime.model_validate({"env": {name: "override"}})
        for payload in (
            {"seed_rows": [{"table": "users", "values": {"id": 1}}]},
            {"before_upgrade_revision": "head"},
            {
                "before_upgrade_revision": "0001_old",
                "seed_rows": [{"table": "alembic_version", "values": {"version_num": "wrong"}}],
            },
            {
                "before_upgrade_revision": "0001_old",
                "seed_rows": [{"table": "users", "values": {"id": [1, 2]}}],
            },
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                QARuntime.model_validate(payload)

    def test_pass_requires_matching_trusted_setup_evidence(self):
        suite = runtime_suite()
        manifest = unit_manifest(suite)
        identity = {
            key: manifest[key]
            for key in ("execution_id", "code_item_id", "source_hash", "suite_hash")
        }
        with self.assertRaisesRegex(BusinessException, "场景启动"):
            validate_manifest(manifest, identity, suite)
        manifest["results"][0]["runtime_evidence"] = {
            "database": "isolated_sqlite",
            "startup_env_names": ["APP_MODE", "SEED_SAMPLE_DATA"],
            "before_upgrade_revision": "0001_old",
            "seed_rows": 1,
            "migrated_to": "head",
        }
        validate_manifest(manifest, identity, suite)
        manifest["results"][0]["runtime_evidence"]["seed_rows"] = 0
        with self.assertRaisesRegex(BusinessException, "场景启动"):
            validate_manifest(manifest, identity, suite)

    def test_old_runner_evidence_is_readable_but_not_current(self):
        suite = unit_suite()
        manifest = unit_manifest(suite)
        manifest["runner_version"] = "forgeai-qa-v2"
        identity = {
            key: manifest[key]
            for key in ("execution_id", "code_item_id", "source_hash", "suite_hash")
        }
        with self.assertRaisesRegex(BusinessException, "执行器版本"):
            validate_manifest(manifest, identity, suite)
        validate_manifest(manifest, identity, suite, allow_legacy=True)

    def test_runtime_timeout_budget_is_bounded(self):
        self.assertEqual(qa_timeout_seconds(unit_suite(), 1200), 300)
        self.assertEqual(qa_timeout_seconds(runtime_suite(), 1200), 360)
        self.assertEqual(qa_timeout_seconds(runtime_suite(), 320), 320)
        raw = runtime_suite().model_dump(mode="json")
        raw["scenarios"] = [
            {**raw["scenarios"][0], "test_id": f"test_runtime_{index}"} for index in range(12)
        ]
        raw["scripts"] = {}
        self.assertEqual(qa_timeout_seconds(QASuite.model_validate(raw), 1200), 900)

    def test_declarative_old_rows_are_inserted_and_rechecked(self):
        runner = trusted_runner()
        with TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.db"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "CREATE TABLE legacy_records (id INTEGER PRIMARY KEY, name TEXT)"
                )
            runner._seed_old_rows(
                database,
                [{"table": "legacy_records", "values": {"id": 1, "name": "old"}}],
            )
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT name FROM legacy_records WHERE id=1").fetchone(),
                    ("old",),
                )
            with self.assertRaisesRegex(ValueError, "表或字段不存在"):
                runner._seed_old_rows(
                    database,
                    [{"table": "legacy_records", "values": {"unknown": "x"}}],
                )
            with self.assertRaisesRegex(ValueError, "不能修改系统表"):
                runner._seed_old_rows(
                    database,
                    [{"table": "alembic_version", "values": {"version_num": "x"}}],
                )

    def test_case_setup_migrates_old_revision_then_head_with_owned_database(self):
        runner = trusted_runner()
        runtime = runtime_suite().scenarios[0].runtime.model_dump(mode="json")
        calls = []
        with TemporaryDirectory() as directory:
            database = Path(directory) / "case.db"

            def migration(argv, env):
                calls.append(argv)
                self.assertEqual(env["DATABASE_URL"], f"sqlite:///{database}")
                if argv == ["upgrade", "0001_old"]:
                    with closing(sqlite3.connect(database)) as connection, connection:
                        connection.execute(
                            "CREATE TABLE legacy_records (id INTEGER PRIMARY KEY, name TEXT)"
                        )
                elif argv == ["upgrade", "head"]:
                    with closing(sqlite3.connect(database)) as connection:
                        self.assertEqual(
                            connection.execute("SELECT name FROM legacy_records").fetchone(),
                            ("old",),
                        )

            with (
                patch.object(runner, "_migration", side_effect=migration),
                patch.object(runner.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
                patch.object(runner, "Path", side_effect=lambda _: database),
            ):
                env, setup = runner.prepare_case_runtime(runtime, 77)
            self.assertEqual(calls, [["upgrade", "0001_old"], ["upgrade", "head"], ["check"]])
            self.assertEqual(env["APP_MODE"], "production")
            self.assertEqual(setup["seed_rows"], 1)
            self.assertNotIn("DATABASE_URL", setup["startup_env_names"])

    def test_runtime_only_suite_skips_default_database_and_keeps_other_results(self):
        runner = trusted_runner()
        suite = runtime_suite().model_dump(mode="json")
        second = copy.deepcopy(suite["scenarios"][0])
        second["test_id"] = "test_second_runtime"
        suite["scenarios"].append(second)
        suite["scripts"][second["test_id"]] = copy.deepcopy(
            suite["scripts"][suite["scenarios"][0]["test_id"]]
        )
        setup = runner.runtime_evidence(second["runtime"])
        payload = {
            "suite": suite,
            "source": {},
            "execution_id": "qa_unit",
            "code_item_id": "ci_unit",
            "source_hash": "a" * 64,
            "suite_hash": "b" * 64,
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backend").mkdir()
            dependency_manifest = "[project]\ndependencies = []\n"
            (root / "backend/pyproject.toml").write_text(dependency_manifest)
            template = root / "template.toml"
            template.write_text(dependency_manifest)
            fake_backend = Mock()
            with (
                patch.object(runner.check, "ROOT", root),
                patch.object(runner.check, "TEMPLATE_BACKEND_PYPROJECT", template),
                patch.object(runner.check, "APPLICATION_UID", None),
                patch.object(runner.check, "materialize"),
                patch.object(runner.os, "chown", create=True),
                patch.object(runner.check, "ensure_backend_dependencies"),
                patch.object(runner.check, "command") as default_migration,
                patch.object(
                    runner, "prepare_case_runtime", return_value=({"APP_MODE": "production"}, setup)
                ),
                patch.object(
                    runner.check,
                    "start_backend",
                    side_effect=[
                        RuntimeError("generated application startup failed"),
                        fake_backend,
                    ],
                ),
                patch.object(runner.check, "stop_process") as stop,
                patch.object(
                    runner,
                    "run_case",
                    return_value={
                        "status": "passed",
                        "failure_kind": None,
                        "observations": [{"kind": "api"}],
                        "evidence": "application assertion passed",
                    },
                ),
            ):
                manifest = runner.execute(payload)
            default_migration.assert_not_called()
            self.assertEqual([row["status"] for row in manifest["results"]], ["failed", "passed"])
            self.assertEqual(manifest["results"][0]["failure_kind"], "product")
            self.assertIn("startup failed", manifest["results"][0]["evidence"])
            self.assertEqual(manifest["results"][1]["runtime_evidence"], setup)
            stop.assert_called_once_with(fake_backend)

    def test_backend_startup_failure_cleans_process_and_rejects_stale_port(self):
        runner = trusted_runner()
        check = runner.check
        process = Mock()
        with (
            patch.object(check.socket, "create_connection", side_effect=ConnectionRefusedError()),
            patch.object(check.subprocess, "Popen", return_value=process),
            patch.object(check, "wait_http", side_effect=RuntimeError("health failed")),
            patch.object(check, "stop_process") as stop,
        ):
            with self.assertRaisesRegex(RuntimeError, "health failed"):
                check.start_backend(port=8000)
            stop.assert_called_once_with(process)

        with (
            patch.object(check.socket, "create_connection", side_effect=ConnectionRefusedError()),
            patch.object(check.subprocess, "Popen", return_value=process),
            patch.object(check, "wait_http"),
            patch.object(check.urllib.request, "urlopen", side_effect=OSError("openapi failed")),
            patch.object(check, "stop_process") as stop,
        ):
            with self.assertRaisesRegex(OSError, "openapi failed"):
                check.start_backend(port=8000)
            stop.assert_called_once_with(process)

        http_error = urllib.error.HTTPError(
            "http://127.0.0.1:8000/openapi.json", 500, "server error", {}, None
        )
        self.assertEqual(runner._runtime_failure(http_error)["failure_kind"], "product")

        raced = Mock()
        raced.poll.side_effect = [None, 0]
        raced.terminate.side_effect = OSError("already exited")
        check.stop_process(raced)
        raced.wait.assert_called_once_with()

        process.poll.return_value = None
        with (
            patch.object(check.socket, "create_connection", side_effect=ConnectionRefusedError()),
            patch.object(check.subprocess, "Popen", return_value=process),
            patch.object(check, "wait_http", side_effect=RuntimeError("original startup failure")),
            patch.object(check, "stop_process", side_effect=OSError("cleanup raced")),
        ):
            with self.assertRaisesRegex(RuntimeError, "original startup failure"):
                check.start_backend(port=8000)
            process.kill.assert_called_once_with()

        occupied = MagicMock()
        with (
            patch.object(check.socket, "create_connection", return_value=occupied),
            patch.object(check.subprocess, "Popen") as launch,
        ):
            with self.assertRaisesRegex(OSError, "已被占用"):
                check.start_backend(port=8000)
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
