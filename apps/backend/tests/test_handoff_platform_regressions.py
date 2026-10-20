"""Regression evidence for the platform blockers in the October 16 handoff."""

import importlib.util
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import sqlalchemy as sa
import test_sandbox_smoke_manifest as smoke_tests
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.core.exceptions import BusinessException, ModelProtocolException
from app.core.llm import chat_with_tools
from app.generation.delivery import WorkItem
from app.generation.engineering.actions import prepare_action
from app.generation.engineering.checkpoint import load_checkpoint, new_checkpoint
from app.generation.engineering.checks import apply_check_outcome
from app.generation.engineering.work_memory import (
    implementation_stalled,
    progress_guidance,
    record_progress,
)
from app.generation.workspace import create_workspace
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.tools.checks import run_check
from app.tools.paths import sha256_bytes
from app.tools.project_deps import _normalize_packages
from sandbox.smoke_manifest import validate_smoke_manifest

sandbox_check = smoke_tests.sandbox_check


class HandoffRegressions(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.manifest = smoke_tests.SmokeManifestTests().baseline()
        self.manifest.pop("visual_contract")

    def validate(self):
        return validate_smoke_manifest(self.manifest, root=self.root, template_metadata={})

    @unittest.skipUnless(os.getenv("FORGEAI_TEST_DOCKER") == "1", "requires Docker check image")
    def test_real_browser_ui_and_expected_409_preserve_application_errors(self):
        root = self.root / "workspace"
        create_workspace(root)
        app = root / "frontend/src/App.tsx"
        source = """import { useState } from 'react'
export function HomePage() {
  const [status, setStatus] = useState('Ready')
  return <main><h1>Negative smoke</h1><p>{status}</p>
    <button id="conflict" onClick={async () => {
      const response = await fetch('/api/v1/conflict', {method: 'POST'})
      setStatus('Expected ' + response.status)
      // ERROR_INJECTION
    }}>Submit</button></main>
}
"""
        app.write_text(source, encoding="utf-8")
        backend = root / "backend/app/main.py"
        backend.write_text(
            backend.read_text(encoding="utf-8")
            + """
@app.post("/api/v1/conflict", status_code=409)
def expected_conflict():
    return {"detail": "duplicate"}
""",
            encoding="utf-8",
        )
        path = root / "forgeai.smoke.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["browser_checks"] = [
            {"id": "ui", "kind": "ui", "route": "/", "expect_text": ["Negative smoke"]},
            {
                "id": "negative",
                "route": "/",
                "expect_text": ["Negative smoke"],
                "expect_api": [{"method": "POST", "path": "/api/v1/conflict", "status": 409}],
                "actions": [
                    {"type": "click", "selector": "#conflict"},
                    {"type": "expect_text", "text": "Expected 409"},
                ],
            },
        ]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        result = run_check(root, check_id="all")
        self.assertTrue(result.ok, result.model_dump())
        app.write_text(
            source.replace("// ERROR_INJECTION", "console.error('genuine failure')"),
            encoding="utf-8",
        )
        failed = run_check(root, check_id="all")
        self.assertFalse(failed.ok)
        self.assertIn("genuine failure", failed.data.get("output", ""))

    def test_long_journey_and_more_cases_share_original_total_action_budget(self):
        case = self.manifest["browser_checks"][0]
        case["actions"] = [{"type": "expect_path", "path": "/items"}] * 74
        self.manifest["browser_checks"] += [
            {"id": f"ui_{i}", "route": "/", "kind": "ui", "expect_text": ["Home"]}
            for i in range(25)
        ]
        self.validate()
        self.manifest["browser_checks"] += [{**case, "id": f"large_{i}"} for i in range(5)]
        with self.assertRaisesRegex(ValueError, "总数 444.*400"):
            self.validate()

    def test_all_quantity_conflicts_report_actual_counts_and_case_identity(self):
        case = self.manifest["browser_checks"][0]
        case["actions"] = [{"type": "expect_path", "path": "/items"}] * 81
        self.manifest["browser_checks"] = [{**case, "id": f"case_{i}"} for i in range(51)]
        with self.assertRaises(ValueError) as error:
            self.validate()
        for expected in ("51", "50", "case_0", "81", "80", "4131", "400"):
            self.assertIn(expected, str(error.exception))

    def test_ui_requires_explicit_kind_and_observation(self):
        case = self.manifest["browser_checks"][0]
        case.pop("expect_api")
        with self.assertRaisesRegex(ValueError, "expect_api"):
            self.validate()
        case["kind"] = "ui"
        self.validate()
        case["actions"] = [{"type": "navigate", "url": "/"}]
        case["expect_text"] = []
        with self.assertRaisesRegex(ValueError, "可观察断言"):
            self.validate()

    def test_negative_response_contract_is_exact_and_rejects_unsafe_paths(self):
        case = self.manifest["browser_checks"][0]
        case["expect_api"] = [{"method": "POST", "path": "/api/v1/apply", "status": 409}]
        self.validate()
        for path in ("//external/", "/\\external/", "https://external/", "/api/v1/\n"):
            with self.subTest(path=path):
                case["expect_api"][0]["path"] = path
                with self.assertRaises(ValueError):
                    self.validate()

    def test_invalid_smoke_write_preserves_live_bytes_and_never_commits(self):
        create_workspace(self.root / "workspace")
        root = self.root / "workspace"
        original = (root / "forgeai.smoke.json").read_bytes()
        digest = sha256_bytes(original)
        action = prepare_action(
            ToolCall(
                id="write",
                name="write_file",
                arguments={
                    "path": "forgeai.smoke.json",
                    "content": '{"version":1}',
                    "expected_hash": digest,
                },
            ),
            root,
            {"forgeai.smoke.json": digest},
        )
        self.assertFalse(action.result.ok)
        self.assertFalse(action.updates)
        self.assertEqual((root / "forgeai.smoke.json").read_bytes(), original)

    def test_invalid_manifest_preflight_never_starts_docker(self):
        root = self.root / "workspace"
        create_workspace(root)
        (root / "forgeai.smoke.json").write_text('{"version":1}', encoding="utf-8")
        with (
            patch("app.tools.checks.shutil.which", return_value="docker"),
            patch("app.tools.checks._execute") as execute,
        ):
            result = run_check(root, check_id="all")
        execute.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.data["phase"], "smoke_manifest")
        self.assertTrue(result.data["source_hash"])

    def test_manifest_and_compile_fail_before_database_setup(self):
        with (
            patch.object(sandbox_check, "load_smoke_manifest", side_effect=ValueError("bad")),
            patch.object(sandbox_check, "database") as database,
        ):
            with self.assertRaisesRegex(ValueError, "bad"):
                sandbox_check.runtime_smoke()
            database.assert_not_called()
        with (
            patch.object(sandbox_check, "load_smoke_manifest", return_value={}),
            patch.object(sandbox_check, "validate_visual_contract"),
            patch.object(sandbox_check, "frontend", side_effect=ValueError("missing export")),
            patch.object(sandbox_check, "database") as database,
        ):
            with self.assertRaisesRegex(ValueError, "missing export"):
                sandbox_check.runtime_smoke()
            database.assert_not_called()

    def test_edits_and_unrelated_checks_cannot_reset_unresolved_check_clock(self):
        checkpoint = new_checkpoint()
        checkpoint.update(current_work_item_id="auth", needs_delivery_plan=False)
        failure = ToolExecutionResult(
            tool_call_id="c",
            name="run_check",
            ok=False,
            summary="failed",
            data={"check_id": "all", "source_hash": "old"},
        )
        record_progress(checkpoint, failure)
        edit = ToolExecutionResult(tool_call_id="w", name="write_file", ok=True, summary="edited")
        for _ in range(48):
            record_progress(checkpoint, edit, changed=True)
        self.assertIsNotNone(progress_guidance(checkpoint))
        checkpoint.update(progress_guidance_turn=1, model_turns=4)
        loaded = load_checkpoint(json.loads(json.dumps({"checkpoint": checkpoint})))
        self.assertTrue(implementation_stalled(loaded))
        passing = failure.model_copy(
            update={"ok": True, "data": {"check_id": "quick", "source_hash": "new"}}
        )
        record_progress(loaded, passing)
        self.assertTrue(implementation_stalled(loaded))
        passing.data["check_id"] = "all"
        record_progress(loaded, passing)
        self.assertFalse(implementation_stalled(loaded))

    def test_completion_activity_identifies_completed_item_even_after_advancing(self):
        first = WorkItem(id="first", title="First", requirement_ids=["f"])
        second = WorkItem(id="second", title="Second", requirement_ids=["s"], depends_on=["first"])
        checkpoint = new_checkpoint()
        checkpoint.update(
            work_items=[first.model_dump(), second.model_dump()], current_work_item_id="first"
        )
        apply_check_outcome(
            checkpoint,
            work_item=first,
            result=ToolExecutionResult(
                tool_call_id="c",
                name="run_check",
                ok=True,
                summary="pass",
                data={"source_hash": "hash", "check_id": "all"},
            ),
            max_repair_rounds=3,
            check_runtime_version="19",
        )
        self.assertEqual(checkpoint["current_work_item_id"], "second")
        self.assertEqual(checkpoint["activity"][-1]["work_item_id"], "first")

    def test_reasoning_exhaustion_preserves_counts_without_exposing_reasoning(self):
        payload = {
            "model": "test",
            "usage": {
                "completion_tokens": 16384,
                "completion_tokens_details": {"reasoning_tokens": 16384},
            },
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": "", "reasoning_content": "PRIVATE"},
                }
            ],
        }
        with (
            patch("app.core.llm._post_chat", return_value=payload),
            patch("app.core.llm.record_model_turn") as meter,
            self.assertRaises(ModelProtocolException) as error,
        ):
            chat_with_tools(messages=[], tools=[], thinking=True)
        meter.assert_called_once()
        self.assertEqual(error.exception.data["truncation_kind"], "reasoning_budget")
        self.assertEqual(error.exception.data["reasoning_tokens"], 16384)
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertNotIn("缩小", str(error.exception))

    def test_dependency_specs_use_their_manager_and_stay_registry_only(self):
        for spec in ("httpx>=0.28,<1", "uvicorn[standard]>=0.30", "pydantic!=2.0,~=2.1"):
            self.assertEqual(_normalize_packages([spec], "uv", "add"), [spec])
        self.assertEqual(
            _normalize_packages(["@types/node@^22"], "npm", "add"), ["@types/node@^22"]
        )
        for manager, spec in (
            ("uv", "x @ https://example.com/x.whl"),
            ("uv", "../x"),
            ("uv", "--index-url=x"),
            ("npm", "x@file:../x"),
            ("npm", "x; echo hi"),
        ):
            with self.subTest(spec=spec), self.assertRaises(BusinessException):
                _normalize_packages([spec], manager, "add")

    def test_legacy_migration_keeps_current_project_data_and_replays_partial_upgrade(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "alembic/versions/b5d7f9a1c3e5_retire_legacy_project_payloads.py"
        )
        spec = importlib.util.spec_from_file_location("retire_legacy", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(
                sa.text("CREATE TABLE project (id INTEGER PRIMARY KEY, name TEXT, prd TEXT)")
            )
            connection.execute(sa.text("INSERT INTO project VALUES (1, 'keep', 'obsolete')"))
            with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
                migration.upgrade()
                migration.upgrade()
                self.assertEqual(
                    connection.execute(sa.text("SELECT id, name FROM project")).one(), (1, "keep")
                )
                self.assertEqual(
                    {c["name"] for c in sa.inspect(connection).get_columns("project")},
                    {"id", "name"},
                )
                migration.downgrade()
                self.assertIsNone(connection.execute(sa.text("SELECT prd FROM project")).scalar())


if __name__ == "__main__":
    unittest.main()
