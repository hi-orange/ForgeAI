"""Check isolation, evidence and environment failures without running generated code on the host."""

import base64
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.core.exceptions import ConflictException
from app.generation.lock import workspace_lock
from app.generation.workspace import create_workspace
from app.tools.checks import (
    CHECK_RUNTIME_VERSION,
    _execute,
    capture_screenshots,
    check_environment,
    docker_command,
    load_smoke_contract,
    run_check,
    source_snapshot,
    visual_docker_command,
)


class EngineeringCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())) / "app"
        create_workspace(self.root)

    def test_snapshot_excludes_secrets_and_changes_identity_with_source(self):
        (self.root / "backend/.env").write_text("SECRET=private", encoding="utf-8")
        (self.root / "backend/data").mkdir(exist_ok=True)
        (self.root / "backend/data/private.json").write_text("secret", encoding="utf-8")
        payload, before = source_snapshot(self.root)
        files = json.loads(payload)
        self.assertNotIn("backend/.env", files)
        self.assertNotIn("backend/data/private.json", files)
        self.assertIn("backend/alembic/env.py", files)
        self.assertIn("template.json", files)
        (self.root / "build.config.json").write_text('{"target":"app"}', encoding="utf-8")
        self.assertIn("build.config.json", json.loads(source_snapshot(self.root)[0]))
        (self.root / "frontend/src/App.tsx").write_text("changed", encoding="utf-8")
        self.assertNotEqual(source_snapshot(self.root)[1], before)

    def test_smoke_contract_is_the_authoritative_action_protocol(self):
        contract = load_smoke_contract()
        browser = contract["browser"]
        self.assertIn("navigate", browser["actions"])
        self.assertEqual(browser["actions"]["expect_count"]["fields"]["count"]["type"], "integer")

    def test_snapshot_includes_only_bounded_public_binary_images(self):
        image = self.root / "frontend/public/images/hero.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"\xff\xd8\xffgenerated")
        private = self.root / "backend/private.jpg"
        private.write_bytes(b"\xff\xd8\xffprivate")

        payload, before = source_snapshot(self.root)
        files = json.loads(payload)

        encoded = files["frontend/public/images/hero.jpg"]
        self.assertEqual(encoded["encoding"], "base64")
        self.assertEqual(base64.b64decode(encoded["content"]), image.read_bytes())
        self.assertNotIn("backend/private.jpg", files)

        image.write_bytes(b"\xff\xd8\xffchanged")
        self.assertNotEqual(source_snapshot(self.root)[1], before)

    def test_container_has_no_host_mount_and_bounded_resources(self):
        command = docker_command("docker", "trusted-image", "owned-instance", "all")
        for flag in [
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--pull=never",
        ]:
            self.assertIn(flag, command)
        self.assertNotIn("--network=none", command)
        self.assertNotIn("--volume", command)
        self.assertNotIn("--mount", command)
        self.assertTrue(any(arg.startswith("--memory=") for arg in command))
        self.assertTrue(any(arg.startswith("--pids-limit=") for arg in command))
        self.assertIn("--pids-limit=256", command)

    def test_visual_container_is_copyable_without_host_mount(self):
        command = visual_docker_command("docker", "trusted-image", "visual-instance", "/jobs")
        self.assertNotIn("--rm", command)
        self.assertNotIn("--read-only", command)
        self.assertFalse(any(arg.startswith("--tmpfs=/tmp:") for arg in command))
        self.assertNotIn("--volume", command)
        self.assertNotIn("--mount", command)
        self.assertIn("--pids-limit=256", command)
        self.assertNotIn("--pids-limit=128", command)
        self.assertEqual(command[-2:], ["visual", "/jobs"])

    def test_capture_screenshots_returns_real_artifact_metadata(self):
        screenshots = [
            {
                "viewport": "desktop",
                "path": "forgeai/evidence/hash/desktop.png",
                "horizontal_overflow": False,
                "console_errors": [],
                "page_errors": [],
            },
            {
                "viewport": "mobile",
                "path": "forgeai/evidence/hash/mobile.png",
                "horizontal_overflow": False,
                "console_errors": [],
                "page_errors": [],
            },
        ]
        with (
            patch("app.tools.checks.shutil.which", return_value="docker"),
            patch("app.tools.checks._execute", return_value=(0, "captured", False)),
            patch(
                "app.tools.checks._collect_visual_evidence",
                return_value=(screenshots, {"browser": "chromium"}),
            ),
            patch("app.tools.checks.subprocess.run"),
        ):
            result = capture_screenshots(self.root, route="/jobs")
        self.assertTrue(result.ok, result.model_dump())
        self.assertEqual(result.data["route"], "/jobs")
        self.assertTrue(result.data["check_id"].startswith("visual_"))
        self.assertEqual(result.data["screenshots"], screenshots)

    def test_capture_screenshots_rejects_external_url(self):
        with patch("app.tools.checks._execute") as execute:
            result = capture_screenshots(self.root, route="https://example.com")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_ROUTE")
        execute.assert_not_called()

    def test_capture_screenshots_classifies_old_runtime_as_environment_failure(self):
        with (
            patch("app.tools.checks.shutil.which", return_value="docker"),
            patch(
                "app.tools.checks._execute",
                return_value=(1, 'raise ValueError("Unknown check")', False),
            ),
            patch("app.tools.checks.subprocess.run"),
        ):
            result = capture_screenshots(self.root)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CHECK_ENVIRONMENT_UNAVAILABLE")
        self.assertIn("版本过旧", result.summary)

    def test_missing_environment_does_not_report_success(self):
        with patch("app.tools.checks.shutil.which", return_value=None):
            result = run_check(self.root, check_id="all")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CHECK_ENVIRONMENT_UNAVAILABLE")

    def test_environment_preflight_requires_daemon_and_pinned_image(self):
        daemon = subprocess.CompletedProcess(
            args=["docker", "info"], returncode=0, stdout="27.5.1\n", stderr=""
        )
        image = subprocess.CompletedProcess(
            args=["docker", "image", "inspect"],
            returncode=0,
            stdout=CHECK_RUNTIME_VERSION,
            stderr="",
        )
        with (
            patch("app.tools.checks.shutil.which", return_value="docker"),
            patch("app.tools.checks.subprocess.run", side_effect=[daemon, image]) as run,
        ):
            result = check_environment()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["runtime_version"], CHECK_RUNTIME_VERSION)
        self.assertEqual(run.call_args_list[0].args[0][0:2], ["docker", "info"])
        self.assertEqual(run.call_args_list[1].args[0][0:3], ["docker", "image", "inspect"])

    def test_environment_preflight_rejects_stale_image(self):
        daemon = subprocess.CompletedProcess(
            args=["docker", "info"], returncode=0, stdout="27.5.1\n", stderr=""
        )
        stale_image = subprocess.CompletedProcess(
            args=["docker", "image", "inspect"], returncode=0, stdout="<no value>\n", stderr=""
        )
        with (
            patch("app.tools.checks.shutil.which", return_value="docker"),
            patch("app.tools.checks.subprocess.run", side_effect=[daemon, stale_image]),
        ):
            result = check_environment()
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CHECK_ENVIRONMENT_UNAVAILABLE")
        self.assertIn("版本过旧", result.summary)

    def test_environment_preflight_stops_when_daemon_is_unavailable(self):
        daemon = subprocess.CompletedProcess(
            args=["docker", "info"], returncode=1, stdout="", stderr="daemon unavailable"
        )
        with (
            patch("app.tools.checks.shutil.which", return_value="docker"),
            patch("app.tools.checks.subprocess.run", return_value=daemon) as run,
        ):
            result = check_environment()
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CHECK_ENVIRONMENT_UNAVAILABLE")
        self.assertIn("Docker daemon", result.summary)
        run.assert_called_once()

    def test_unknown_check_never_launches_process(self):
        with patch("app.tools.checks._execute") as execute:
            result = run_check(self.root, check_id="all; rm -rf /")
        execute.assert_not_called()
        self.assertEqual(result.error_code, "UNKNOWN_CHECK")

    def test_check_returns_logs_and_cleans_its_own_container(self):
        with (
            patch("app.tools.checks.shutil.which", return_value="docker"),
            patch("app.tools.checks._execute", return_value=(1, "migration failed", False)),
            patch("app.tools.checks.subprocess.run") as cleanup,
        ):
            result = run_check(self.root, check_id="database")
        self.assertFalse(result.ok)
        self.assertEqual(result.data["output"], "migration failed")
        self.assertEqual(result.data["source_hash"], source_snapshot(self.root)[1])
        self.assertEqual(cleanup.call_args.args[0][0:3], ["docker", "rm", "-f"])
        self.assertTrue(cleanup.call_args.args[0][-1].startswith("forgeai-check-"))

    def test_security_failure_has_distinct_error_code(self):
        with (
            patch("app.tools.checks.shutil.which", return_value="docker"),
            patch("app.tools.checks._execute", return_value=(3, "blocked", False)),
            patch("app.tools.checks.subprocess.run"),
        ):
            result = run_check(self.root, check_id="security")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "SECURITY_CHECK_FAILED")

    def test_timeout_also_removes_container(self):
        with (
            patch("app.tools.checks.shutil.which", return_value="docker"),
            patch("app.tools.checks._execute", side_effect=subprocess.TimeoutExpired("docker", 1)),
            patch("app.tools.checks.subprocess.run") as cleanup,
        ):
            result = run_check(self.root, check_id="all")
        self.assertEqual(result.error_code, "CHECK_TIMEOUT")
        cleanup.assert_called_once()

    def test_bounded_output_keeps_tail(self):
        code, output, truncated = _execute(
            [sys.executable, "-c", "print('x' * 100000); print('TAIL')"], b"", 10
        )
        self.assertEqual(code, 0)
        self.assertTrue(truncated)
        self.assertLessEqual(len(output), 32768)
        self.assertIn("TAIL", output)

    def test_workspace_lock_excludes_second_writer_and_releases(self):
        with workspace_lock(self.root):
            with self.assertRaises(ConflictException), workspace_lock(self.root):
                self.fail("Second writer acquired the same workspace")
        with workspace_lock(self.root):
            pass

    @unittest.skipUnless(
        os.getenv("FORGEAI_TEST_DOCKER") == "1", "requires built check image and Docker"
    )
    def test_real_template_checks_and_browser_screenshots(self):
        security = run_check(self.root, check_id="security")
        quick = run_check(self.root, check_id="quick")
        self.assertTrue(quick.ok, quick.model_dump())
        self.assertIn("tsc", quick.data["output"])
        self.assertTrue(security.ok, security.model_dump())
        self.assertIn("FORGEAI_SECURITY_REPORT=", security.data["output"])
        result = run_check(self.root, check_id="all")
        self.assertTrue(result.ok, result.model_dump())
        self.assertIn("实际表结构", result.data["output"])
        visual = capture_screenshots(self.root)
        self.assertTrue(visual.ok, visual.model_dump())
        self.assertEqual(
            {item["viewport"] for item in visual.data["screenshots"]},
            {"desktop", "mobile"},
        )
        for item in visual.data["screenshots"]:
            self.assertTrue((self.root / item["path"]).is_file())

    @unittest.skipUnless(
        os.getenv("FORGEAI_TEST_DOCKER") == "1", "requires built check image and Docker"
    )
    def test_real_cross_file_route_and_migration_failures_reach_repair_context(self):
        from app.generation.engineering.checkpoint import new_checkpoint
        from app.orchestration.code_engineer import _EngineeringNodes

        checkpoint = new_checkpoint()
        caller = self.root / "frontend/src/regression.tsx"
        caller.write_text(
            "import { Link } from '@tanstack/react-router'\n"
            'export function RegressionLink() { return <Link to="/regression">Open</Link> }\n',
            encoding="utf-8",
        )
        failed = run_check(self.root, check_id="quick")
        self.assertFalse(failed.ok, failed.model_dump())
        _EngineeringNodes._remember_check(checkpoint, "quick", failed)
        self.assertEqual(
            checkpoint["diagnostic_state"]["check:quick"]["diagnostic"]["category"], "build"
        )
        self.assertTrue(
            any(d["code"] == "TS2322" for d in checkpoint["repair_context"]["diagnostics"])
        )
        router = self.root / "frontend/src/router.tsx"
        source = router.read_text(encoding="utf-8")
        source = source.replace(
            "const routeTree = rootRoute.addChildren([indexRoute])",
            "const regressionRoute = createRoute({ getParentRoute: () => rootRoute, "
            "path: '/regression', component: () => <div>Regression</div> })\n"
            "const routeTree = rootRoute.addChildren([indexRoute, regressionRoute])",
        )
        router.write_text(source, encoding="utf-8")
        passed = run_check(self.root, check_id="quick")
        self.assertTrue(passed.ok, passed.model_dump())
        _EngineeringNodes._remember_check(checkpoint, "quick", passed)
        self.assertEqual(checkpoint["diagnostic_state"]["check:quick"]["status"], "resolved")
        self.assertNotEqual(failed.data["source_hash"], passed.data["source_hash"])
        self.assertIn('to="/regression"', caller.read_text(encoding="utf-8"))

        model = self.root / "backend/app/models/meta.py"
        model.write_text(
            model.read_text(encoding="utf-8").replace(
                "mapped_column(String(255), nullable=False)",
                "mapped_column(String(255), nullable=False, index=True, unique=True)",
            ),
            encoding="utf-8",
        )
        failed = run_check(self.root, check_id="database")
        self.assertFalse(failed.ok, failed.model_dump())
        _EngineeringNodes._remember_check(checkpoint, "database", failed)
        self.assertEqual(
            checkpoint["diagnostic_state"]["check:database"]["diagnostic"]["category"], "database"
        )
        self.assertIn(
            "ix_app_meta_value", checkpoint["repair_context"]["failed_check"]["data"]["output"]
        )
        migration = self.root / "backend/alembic/versions/0002_value_index.py"
        migration.write_text(
            'from alembic import op\nrevision = "0002_value_index"\n'
            'down_revision = "0001_create_app_meta"\n'
            "branch_labels = None\ndepends_on = None\n"
            "def upgrade():\n"
            '    op.create_index("ix_app_meta_value", "app_meta", ["value"], unique=True)\n'
            'def downgrade():\n    op.drop_index("ix_app_meta_value", table_name="app_meta")\n',
            encoding="utf-8",
        )
        passed = run_check(self.root, check_id="database")
        self.assertTrue(passed.ok, passed.model_dump())
        _EngineeringNodes._remember_check(checkpoint, "database", passed)
        self.assertEqual(checkpoint["diagnostic_state"]["check:database"]["status"], "resolved")
        self.assertNotEqual(failed.data["source_hash"], passed.data["source_hash"])
