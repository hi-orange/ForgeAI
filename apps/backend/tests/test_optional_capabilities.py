"""Opt-in capability selection, fenced installation and real local auth behavior."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from test_architect import valid_design
from test_product_manager_workflow import valid_spec

from app.generation.capabilities import (
    CAPABILITY_ROOT,
    capability_entry_paths,
    freeze_optional_capabilities,
)
from app.generation.engineering.actions import commit_action, prepare_action
from app.generation.workspace import create_workspace
from app.schemas.agent_action import ToolCall
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.services import architect as architect_service
from app.services.engineering.claim import build_frozen_input_snapshot
from app.tools.architect import ArchitectToolState, execute_architect_tool
from app.tools.capabilities import prepare_capability
from app.tools.paths import sha256_bytes


class OptionalCapabilityTests(unittest.TestCase):
    def test_generic_action_cannot_install_without_frozen_capability_inputs(self):
        call = ToolCall(
            id="install", name="install_capability", arguments={"capability_id": "auth-session-v1"}
        )
        action = prepare_action(call, self.root, self.hashes)
        self.assertFalse(action.result.ok)
        self.assertIn("冻结输入", action.result.summary)
        self.assertFalse(action.updates)

    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory(prefix="forgeai-capability-")))
        self.root = create_workspace(self.directory / "workspace")
        self.spec = AppSpec.model_validate(valid_spec())
        design = valid_design()
        design["optional_capabilities"] = [
            {
                "capability_id": "auth-session-v1",
                "requirement_ids": [self.spec.features[0].id],
                "rationale": "Approved local identity contract",
            }
        ]
        self.design = SystemDesign.model_validate(design)
        self.frozen = freeze_optional_capabilities(self.spec, self.design)
        self.hashes = {
            path: sha256_bytes((self.root / path).read_bytes()) for path in capability_entry_paths()
        }

    def prepare(self):
        return prepare_capability(
            ToolCall(
                id="install",
                name="install_capability",
                arguments={"capability_id": "auth-session-v1"},
            ),
            self.root,
            self.hashes,
            spec=self.spec,
            system_design=self.design,
            capability_snapshot=self.frozen,
        )

    def test_simple_site_has_no_auth_and_cannot_install_unselected_capability(self):
        self.design.optional_capabilities = []
        self.frozen = freeze_optional_capabilities(self.spec, self.design)
        self.assertEqual(self.frozen, [])
        self.assertEqual(freeze_optional_capabilities(self.spec, None), [])
        self.assertFalse((self.root / "backend/app/api/auth.py").exists())
        self.assertFalse(self.prepare().result.ok)
        self.assertNotIn(
            "include_router(auth.router)", (self.root / "backend/app/api/router.py").read_text()
        )

    def test_capability_selection_must_reference_approved_feature_ids(self):
        self.design.optional_capabilities[0].requirement_ids = ["unapproved_feature"]
        with self.assertRaisesRegex(ValueError, "获批功能"):
            freeze_optional_capabilities(self.spec, self.design)
        self.design.optional_capabilities[0].requirement_ids = [self.spec.features[0].id]
        self.design.optional_capabilities.append(self.design.optional_capabilities[0])
        with self.assertRaisesRegex(ValueError, "重复"):
            freeze_optional_capabilities(self.spec, self.design)

    def test_architect_incremental_selection_is_validated_at_final_submission(self):
        state = ArchitectToolState(app_spec=self.spec, workspace_root=self.root)

        def call(name, **arguments):
            return execute_architect_tool(
                ToolCall(id="draft", name=name, arguments=arguments), state
            )

        self.assertTrue(call("read_artifact")[0].ok)
        self.assertTrue(call("editor_patch", sections=valid_design())[0].ok)
        selection = self.design.optional_capabilities[0].model_dump(mode="json")
        self.assertTrue(call("editor_patch", sections={"optional_capabilities": [selection]})[0].ok)
        result, submitted = call("write_system_design")
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(submitted.optional_capabilities, self.design.optional_capabilities)
        selection["requirement_ids"] = ["unapproved_feature"]
        self.assertTrue(call("editor_patch", sections={"optional_capabilities": [selection]})[0].ok)
        self.assertFalse(call("write_system_design")[0].ok)

    def test_claim_freezes_selected_source_version_and_old_inputs_do_not_opt_in(self):
        snapshot = build_frozen_input_snapshot(
            project_id=1,
            run_id="run",
            task_id="task",
            approved_item_id="approved",
            spec=self.spec,
            design_item_id="design",
            system_design=self.design,
        )
        self.assertEqual(snapshot["optional_capabilities"], self.frozen)
        self.frozen = None
        self.assertFalse(self.prepare().result.ok)

    def test_empty_capability_selection_preserves_legacy_design_publication_identity(self):
        legacy = valid_design()
        legacy.pop("implementation_dependencies", None)
        legacy.pop("optional_capabilities", None)
        digest = hashlib.sha256(
            json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        db = MagicMock()
        db.get.return_value = SimpleNamespace(result_hash=digest, configuration_item_id="ci_legacy")
        existing = SimpleNamespace(item_id="ci_legacy", payload=legacy)
        db.scalar.return_value = existing
        with (
            patch.object(
                architect_service.task_execution,
                "lock_run",
                return_value=SimpleNamespace(stage="architect"),
            ),
            patch.object(architect_service, "_load_task", return_value=(MagicMock(), MagicMock())),
            patch.object(architect_service, "_validate_task", return_value="ci_approved"),
        ):
            result = architect_service.complete_architect_task(
                db, MagicMock(), 1, "run", "task", "execution", SystemDesign.model_validate(legacy)
            )
        self.assertIs(result, existing)
        self.assertNotIn("optional_capabilities", existing.payload)
        db.add.assert_not_called()

    def test_install_stages_sources_and_commits_once(self):
        action = self.prepare()
        self.assertTrue(action.result.ok, action.result.summary)
        self.assertFalse((self.root / "backend/app/api/auth.py").exists())
        result = commit_action(action, self.root, revision_id="capability-install")
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(len(result.data["files"]), 8)
        self.assertIn("auth.router", (self.root / "backend/app/api/router.py").read_text())
        self.assertFalse(self.prepare().result.ok)

    def test_install_requires_reads_and_refuses_custom_entry_or_migration(self):
        self.hashes = {}
        self.assertIn("必须读取", self.prepare().result.summary)
        self.hashes = {
            path: sha256_bytes((self.root / path).read_bytes()) for path in capability_entry_paths()
        }
        migration = self.root / "backend/alembic/versions/0002_business.py"
        migration.write_text("# business", encoding="utf-8")
        self.assertIn("自定义迁移链", self.prepare().result.summary)
        migration.unlink()
        router = self.root / "backend/app/api/router.py"
        router.write_text("# existing custom business", encoding="utf-8")
        self.assertIn("自定义代码", self.prepare().result.summary)
        self.assertFalse((self.root / "backend/app/api/auth.py").exists())

    def test_installer_rejects_changed_bundle_and_concurrent_source(self):
        self.frozen[0]["source_digest"] = "stale"
        self.assertIn("冻结版本", self.prepare().result.summary)
        self.frozen = freeze_optional_capabilities(self.spec, self.design)
        action = self.prepare()
        self.assertTrue(action.result.ok)
        (self.root / "backend/alembic/versions/0002_concurrent.py").write_text("# concurrent")
        result = commit_action(action, self.root, revision_id="conflict")
        self.assertFalse(result.ok)
        self.assertFalse((self.root / "backend/app/api/auth.py").exists())

    def test_auth_bundle_migrations_login_expiry_logout_and_user_isolation(self):
        action = self.prepare()
        result = commit_action(action, self.root, revision_id="runtime-auth")
        self.assertTrue(result.ok, result.summary)
        probe = Path(__file__).parent / "support" / "auth_capability_probe.py"
        environment = os.environ.copy()
        environment["DATABASE_URL"] = "sqlite:///" + (self.directory / "auth.db").as_posix()
        environment["PYTHONPATH"] = str(self.root / "backend")
        result = subprocess.run(
            [sys.executable, str(probe.resolve())],
            cwd=self.root / "backend",
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AUTH_CAPABILITY_RUNTIME_OK", result.stdout)

    def test_bundle_missing_or_unknown_version_is_not_claimed_available(self):
        with patch("app.generation.capabilities.CAPABILITY_ROOT", CAPABILITY_ROOT / "missing"):
            with self.assertRaises(FileNotFoundError):
                freeze_optional_capabilities(self.spec, self.design)

    def test_frontend_does_not_deliver_private_responses_after_session_changes(self):
        typescript = Path(__file__).resolve().parents[2] / "frontend/node_modules/typescript"
        node = shutil.which("node")
        if node is None or not typescript.is_dir():
            self.skipTest("Node and frontend TypeScript dependencies are required for this probe")
        probe = Path(__file__).parent / "support/auth_frontend_probe.cjs"
        source = CAPABILITY_ROOT / "auth-session-v1/frontend/src/lib/auth.tsx"
        result = subprocess.run(
            [node, str(probe), str(source), str(typescript)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AUTH_CAPABILITY_FRONTEND_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
