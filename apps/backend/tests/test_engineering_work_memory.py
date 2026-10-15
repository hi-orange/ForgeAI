"""Working evidence survives history rollover; stalls get bounded corrective action."""

import json
import tempfile
import unittest
from pathlib import Path

from test_product_manager_workflow import valid_spec

from app.agents.code_engineer import build_code_engineer_messages
from app.core.exceptions import ConflictException
from app.generation.delivery import WorkItem
from app.generation.engineering.checkpoint import (
    load_checkpoint,
    new_checkpoint,
    prepare_manual_repair_retry,
)
from app.generation.engineering.work_memory import (
    FOCUS_TOOL,
    MAX_RECOVERY_ACTIONS,
    accept_focus,
    correction,
    item_memory,
    record_result,
    request_correction,
    tool_names,
    validate_action,
    validate_focus,
    working_context,
)
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.tools.files import read_file
from app.tools.paths import sha256_bytes


class WorkMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = new_checkpoint()
        self.c.update(current_work_item_id="auth", needs_delivery_plan=False)
        self.args = dict(
            work_item_id="auth",
            stalled_because="Repeated inspection",
            next_change="Implement endpoint",
            target_paths=["auth.py"],
            read_paths=["user.py"],
            verification="Run backend check",
        )
        self.names = {
            FOCUS_TOOL,
            "read_file",
            "search_code",
            "list_files",
            "write_file",
            "run_check",
            "complete_work_item",
            "report_blocked",
            "update_delivery_plan",
        }

    def read(self, path="user.py", content="VALUE = 1"):
        (self.root / path).write_text(content, encoding="utf-8")
        result = read_file(self.root, path=path).model_copy(update={"arguments": {"path": path}})
        record_result(self.c, result)
        return result

    def focus(self):
        request_correction(self.c, "Repeated reads")
        validate_focus(self.c, self.args, self.root)
        accept_focus(self.c, self.args)

    def test_evidence_and_progress_survive_rollover_and_recovery(self):
        self.read()
        result = ToolExecutionResult(
            tool_call_id="write",
            name="write_file",
            ok=True,
            summary="Created auth.py",
            arguments={"path": "auth.py"},
        )
        raw = b"def login(): pass\n"
        (self.root / "auth.py").write_bytes(raw)
        record_result(self.c, result, updates={"auth.py": raw})
        self.c["observations"] = []  # Earlier exchanges no longer fit in rolling history.
        restored = load_checkpoint(json.loads(json.dumps({"checkpoint": self.c})))
        context = working_context(restored, self.root)
        self.assertEqual(context["changed_files"], {"auth.py": sha256_bytes(raw)})
        self.assertEqual({x["path"] for x in context["source_evidence"]}, {"auth.py", "user.py"})
        self.assertEqual(restored["outcome"], "running")

    def test_external_change_or_deletion_never_reuses_stale_source(self):
        for deleted in [False, True]:
            self.read()
            if deleted:
                (self.root / "user.py").unlink()
            else:
                (self.root / "user.py").write_text("VALUE = 2", encoding="utf-8")
            context = working_context(self.c, self.root)
            self.assertEqual(context["source_evidence"], [])
            self.assertEqual(context["stale_evidence_paths"], ["user.py"])

    def test_committed_write_replaces_cached_source_and_hash(self):
        self.read()
        raw = b"VALUE = 2"
        (self.root / "user.py").write_bytes(raw)
        result = ToolExecutionResult(
            tool_call_id="edit", name="edit_file_by_replace", ok=True, summary="Changed user.py"
        )
        record_result(self.c, result, updates={"user.py": raw})
        entry = working_context(self.c, self.root)["source_evidence"][0]
        self.assertEqual(entry["content"], "VALUE = 2")
        self.assertEqual(entry["content_hash"], sha256_bytes(raw))

    def test_failed_write_does_not_record_uncommitted_bytes(self):
        self.read()
        result = ToolExecutionResult(
            tool_call_id="edit",
            name="write_file",
            ok=False,
            error_code="WRITE_CONFLICT",
            summary="stale hash",
        )
        record_result(self.c, result, updates={"user.py": b"WRONG"})
        self.assertEqual(item_memory(self.c)["changed_files"], {})
        self.assertNotIn("WRONG", str(working_context(self.c, self.root)))

    def test_repeated_reads_cannot_evict_written_evidence_and_size_is_bounded(self):
        raw = b"IMPORTANT = 1"
        (self.root / "auth.py").write_bytes(raw)
        record_result(
            self.c,
            ToolExecutionResult(tool_call_id="w", name="write_file", ok=True, summary="written"),
            updates={"auth.py": raw},
        )
        for i in range(30):
            self.read(f"f{i}.py", "x" * 8000)
        self.assertLessEqual(len(self.c["source_evidence"]), 12)
        context = working_context(self.c, self.root)
        self.assertLessEqual(sum(len(x["content"]) for x in context["source_evidence"]), 24000)
        self.assertIn("auth.py", [x["path"] for x in context["source_evidence"]])
        self.assertTrue(context["omitted_evidence_paths"])
        self.assertTrue(any(x["content_truncated"] for x in context["source_evidence"]))

    def test_stall_requires_focus_before_any_more_reads(self):
        for i in range(12):
            self.read(f"f{i}.py")
        self.assertEqual(correction(self.c)["phase"], "awaiting_focus")
        self.assertEqual(tool_names(self.c, self.names), {FOCUS_TOOL, "report_blocked"})
        with self.assertRaises(ConflictException):
            validate_action(
                self.c,
                ToolCall(id="r", name="read_file", arguments={"path": "user.py"}),
                self.names,
            )

    def test_correction_reads_are_targeted_bounded_and_durable(self):
        self.focus()
        with self.assertRaises(ConflictException):
            validate_action(
                self.c,
                ToolCall(id="r", name="read_file", arguments={"path": "unrelated.py"}),
                self.names,
            )
        for _ in range(4):
            call = ToolCall(id="r", name="read_file", arguments={"path": "user.py"})
            validate_action(self.c, call, self.names)
            self.read()
            self.c = json.loads(json.dumps(self.c))
        self.assertNotIn("read_file", tool_names(self.c, self.names))
        self.assertIn("report_blocked", tool_names(self.c, self.names))
        self.assertIn("write_file", tool_names(self.c, self.names))
        self.assertEqual(correction(self.c)["read_calls_remaining"], 0)

    def test_noop_checks_cannot_extend_correction_forever(self):
        self.focus()
        result = ToolExecutionResult(
            tool_call_id="check",
            name="run_check",
            ok=True,
            summary="same check",
            data={"source_hash": "same"},
        )
        outcomes = [record_result(self.c, result) for _ in range(MAX_RECOVERY_ACTIONS)]
        self.assertEqual(outcomes[-1], "exhausted")
        self.assertEqual(correction(self.c)["phase"], "exhausted")

    def test_correction_note_does_not_complete_or_reset_usage(self):
        self.c.update(model_turns=40, tool_calls=80, no_progress_steps=6)
        self.focus()
        self.assertEqual(self.c["outcome"], "running")
        self.assertEqual(self.c["no_progress_steps"], 6)
        self.assertEqual(self.c["model_turns"], 40)
        self.assertEqual(self.c["tool_calls"], 80)

    def test_focus_rejects_other_item_and_paths_outside_workspace(self):
        request_correction(self.c, "stalled")
        for override in [
            {"work_item_id": "other"},
            {"target_paths": ["../outside.py"]},
            {"read_paths": ["F:/outside.py"]},
        ]:
            with self.assertRaises(ConflictException):
                validate_focus(self.c, {**self.args, **override}, self.root)

    def test_explicit_stall_retry_keeps_evidence_and_requires_changed_approach(self):
        self.read()
        self.c.update(
            outcome="blocked",
            blocked_reason_code="NO_SOURCE_PROGRESS",
            blocked_reason="Repeated reads",
            model_turns=40,
            tool_calls=90,
        )
        retried = prepare_manual_repair_retry({"checkpoint": self.c})["checkpoint"]
        self.assertEqual(retried["model_turns"], 40)
        self.assertEqual(retried["tool_calls"], 90)
        self.assertIn("user.py", retried["source_evidence"])
        self.assertEqual(correction(retried)["phase"], "awaiting_focus")
        self.assertEqual(tool_names(retried, self.names), {FOCUS_TOOL, "report_blocked"})

    def test_work_item_switch_does_not_inherit_other_items_stall(self):
        self.focus()
        self.c["current_work_item_id"] = "jobs"
        self.assertEqual(correction(self.c), {})
        self.assertEqual(item_memory(self.c)["changed_files"], {})
        self.assertIn("search_code", tool_names(self.c, self.names))

    def test_messages_supply_evidence_once_and_remove_stale_reads(self):
        result = self.read()
        spec = AppSpec.model_validate(valid_spec())
        item = WorkItem(id="auth", title="Auth", requirement_ids=[spec.features[0].id])
        for stale in [False, True]:
            if stale:
                (self.root / "user.py").unlink()
            context = working_context(self.c, self.root)
            messages = build_code_engineer_messages(
                spec=spec,
                work_item=item,
                system_design=None,
                engineering_context={"working_memory": context},
                observations=[result] * 24,
            )
            self.assertEqual(sum(m["role"] == "tool" for m in messages), 0 if stale else 1)
            payload = json.loads(messages[-1]["content"])
            self.assertEqual(
                len(payload["engineering_context"]["working_memory"]["source_evidence"]),
                0 if stale else 1,
            )

    def test_old_read_exchange_is_not_replayed_after_committed_change(self):
        old = self.read()
        raw = b"VALUE = 2"
        (self.root / "user.py").write_bytes(raw)
        record_result(
            self.c,
            ToolExecutionResult(
                tool_call_id="w", name="write_file", ok=True, summary="Changed user"
            ),
            updates={"user.py": raw},
        )
        spec = AppSpec.model_validate(valid_spec())
        item = WorkItem(id="auth", title="Auth", requirement_ids=[spec.features[0].id])
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=item,
            system_design=None,
            engineering_context={"working_memory": working_context(self.c, self.root)},
            observations=[old],
        )
        self.assertFalse(any(m["role"] == "tool" for m in messages))
        self.assertIn("VALUE = 2", messages[-1]["content"])

    def test_old_checkpoint_upgrade_preserves_terminal_stop_and_budgets(self):
        self.c.update(
            schema_version=13,
            outcome="blocked",
            blocked_reason_code="NO_SOURCE_PROGRESS",
            model_turns=166,
            tool_calls=250,
        )
        del self.c["work_memory"]
        del self.c["source_evidence"]
        loaded = load_checkpoint({"checkpoint": self.c})
        self.assertEqual(loaded["schema_version"], 14)
        self.assertEqual(loaded["outcome"], "blocked")
        self.assertEqual((loaded["model_turns"], loaded["tool_calls"]), (166, 250))


if __name__ == "__main__":
    unittest.main()
