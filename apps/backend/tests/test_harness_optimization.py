"""Contract generation, scoped verification and final gates; no live model required."""

import base64
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.core.exceptions import BusinessException, ConflictException
from app.generation.delivery import WorkItem
from app.generation.engineering.actions import commit_action, prepare_action
from app.generation.engineering.checkpoint import new_checkpoint
from app.generation.engineering.checks import apply_check_outcome, select_completion_check
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.tools.api_client import MARKER, read_generated_files
from app.tools.check_diagnostics import repair_verification
from app.tools.files import restore_workspace_revision


class HarnessOptimizationTests(unittest.TestCase):
    def select(self, state, files, *, kind="feature", final=False, repair=False):
        return select_completion_check(
            state,
            WorkItem(id="work", requirement_ids=["feature"], title="Work", kind=kind),
            files,
            final=final,
            quality_repair=repair,
            runtime_version="20",
        )[0]

    def test_style_only_diff_requires_a_current_full_baseline(self):
        files = {"frontend/src/index.css": "new", "backend/app/main.py": "stable"}
        state = {
            "integration_baseline": {
                "runtime_version": "20",
                "files": {**files, "frontend/src/index.css": "old"},
            }
        }
        self.assertEqual(self.select(state, files), "frontend")
        self.assertEqual(self.select(state, files, final=True), "all")
        self.assertEqual(self.select(state, files, repair=True), "all")
        self.assertEqual(self.select({}, files), "all")
        state["integration_baseline"]["runtime_version"] = "19"
        self.assertEqual(self.select(state, files), "all")

    def test_removed_backend_file_and_unresolved_failure_force_full_check(self):
        files = {"frontend/src/index.css": "new"}
        state = {
            "integration_baseline": {
                "runtime_version": "20",
                "files": {"frontend/src/index.css": "old", "backend/app/main.py": "old"},
            }
        }
        self.assertEqual(self.select(state, files), "all")
        state["integration_baseline"]["files"].pop("backend/app/main.py")
        state["verification_watch"] = {"work": {"all": 2}}
        self.assertEqual(self.select(state, files), "all")

    def test_final_gate_rejects_local_or_old_runner_evidence_without_advancing(self):
        item = WorkItem(id="work", requirement_ids=["feature"], title="Work")
        state = new_checkpoint()
        state["work_items"] = [item.model_dump(mode="json")]
        before = deepcopy(state)
        for check_id, version in (("quick", "20"), ("frontend", "20"), ("all", "19")):
            result = ToolExecutionResult(
                tool_call_id="gate",
                name="run_check",
                ok=True,
                summary="passed",
                data={"source_hash": "hash", "check_id": check_id, "runtime_version": version},
            )
            with self.assertRaises(ConflictException):
                apply_check_outcome(
                    state,
                    work_item=item,
                    result=result,
                    max_repair_rounds=3,
                    check_runtime_version="20",
                )
            self.assertEqual(state, before)

    def test_export_diagnostic_names_both_consumer_and_symbol(self):
        result = ToolExecutionResult(
            tool_call_id="check",
            name="run_check",
            ok=False,
            summary="failed",
            data={
                "check_id": "all",
                "output": "src/pages/Edit.tsx(4,2): error TS2305: "
                "Module '\"../lib/recruiter\"' has no exported member 'fetchRecruiterJob'.",
            },
        )
        plan = repair_verification(result)
        self.assertEqual(plan["check_id"], "quick")
        self.assertFalse(plan["delivery_pass"])
        self.assertEqual(plan["targets"][0]["symbol"], "fetchRecruiterJob")
        self.assertEqual(plan["targets"][0]["related_module"], "../lib/recruiter")
        self.assertEqual(plan["targets"][0]["path"], "frontend/src/pages/Edit.tsx")

    @staticmethod
    def output(extra=None):
        files = {
            "frontend/openapi.json": '{"openapi":"3.1.0","paths":{}}',
            "frontend/src/client/types.gen.ts": "export type Result = string",
            "frontend/src/client/sdk.gen.ts": "export {}",
            **(extra or {}),
        }
        return MARKER + base64.b64encode(json.dumps(files).encode()).decode()

    def test_client_output_cannot_escape_generated_directory(self):
        for path in ("backend/app/main.py", "frontend/src/client/../../e2e/test.ts"):
            with self.assertRaises(BusinessException):
                read_generated_files(self.output({path: "tamper"}))

    def test_client_replacement_is_fenced_and_deletions_are_recoverable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "frontend/src/client/obsolete.ts"
            old.parent.mkdir(parents=True)
            old.write_text("old", encoding="utf-8")
            backend = root / "backend/app/main.py"
            backend.parent.mkdir(parents=True)
            backend.write_text("original", encoding="utf-8")
            call = ToolCall(id="generate", name="generate_api_client", arguments={})
            with (
                patch("app.tools.api_client.get_docker_sandbox_runtime") as runtime,
                patch(
                    "app.tools.api_client.checks._execute", return_value=(0, self.output(), False)
                ),
            ):
                runtime.return_value.require_image.return_value = "docker"
                runtime.return_value.isolation_args.return_value = []
                action = prepare_action(call, root, {})
                self.assertTrue(action.result.ok, action.result.summary)
                backend.write_text("concurrent", encoding="utf-8")
                self.assertFalse(commit_action(action, root, revision_id="conflict").ok)
                self.assertTrue(old.exists())
                action = prepare_action(call, root, {})
                self.assertTrue(commit_action(action, root, revision_id="generated").ok)
            self.assertFalse(old.exists())
            self.assertTrue((root / "frontend/src/client/sdk.gen.ts").exists())
            restore_workspace_revision(root, revision_id="generated")
            self.assertEqual(old.read_text(), "old")
            self.assertFalse((root / "frontend/src/client/sdk.gen.ts").exists())
