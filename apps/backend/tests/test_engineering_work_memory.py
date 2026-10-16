"""Working evidence survives history rollover; observations never restrict tools."""

import json
import tempfile
import unittest
from pathlib import Path

from test_product_manager_workflow import valid_spec

from app.agents.code_engineer import build_code_engineer_messages
from app.generation.delivery import WorkItem
from app.generation.engineering.checkpoint import (
    load_checkpoint,
    new_checkpoint,
    prepare_manual_repair_retry,
)
from app.generation.engineering.work_memory import (
    implementation_stalled,
    item_memory,
    progress_guidance,
    record_progress,
    record_result,
    working_context,
)
from app.schemas.agent_action import ToolExecutionResult
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

    def read(self, path="user.py", content="VALUE = 1"):
        (self.root / path).write_text(content, encoding="utf-8")
        result = read_file(self.root, path=path).model_copy(update={"arguments": {"path": path}})
        record_result(self.c, result)
        return result

    def test_actual_coverage_not_requested_range_defines_new_information(self):
        (self.root / "a.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
        for start, end in [(1, 2), (3, 4)]:
            record_progress(
                self.c, read_file(self.root, path="a.py", start_line=start, end_line=end)
            )
            self.assertEqual(self.c["no_progress_steps"], 0)
        for end in [4, 40, 400]:
            record_progress(self.c, read_file(self.root, path="a.py", end_line=end))
        self.assertEqual(self.c["no_progress_steps"], 3)
        self.assertEqual(self.c["read_coverage"]["a.py"]["ranges"], [[1, 4]])
        (self.root / "a.py").write_text("a\nb\nc\nchanged\n", encoding="utf-8")
        record_progress(self.c, read_file(self.root, path="a.py", start_line=4))
        self.assertEqual(self.c["no_progress_steps"], 0)
        self.assertEqual(self.c["steps_without_delivery"], 6)

    def test_text_omitted_from_model_context_is_not_marked_as_already_read(self):
        body = "".join(f"{i:03d}:" + "x" * 95 + "\n" for i in range(100))
        observation = ToolExecutionResult(
            tool_call_id="read",
            name="read_file",
            ok=True,
            summary="read",
            data={
                "path": "large.py",
                "content_hash": "h",
                "start_line": 1,
                "end_line": 100,
                "content": body,
            },
        )
        record_progress(self.c, observation)
        self.assertEqual(self.c["read_coverage"]["large.py"]["ranges"], [[1, 60]])
        tail = observation.model_copy(
            update={
                "data": {
                    **observation.data,
                    "start_line": 61,
                    "content": "".join(body.splitlines(keepends=True)[60:]),
                }
            }
        )
        record_progress(self.c, tail)
        self.assertEqual(self.c["no_progress_steps"], 0)
        self.assertEqual(self.c["read_coverage"]["large.py"]["ranges"], [[1, 100]])
        record_progress(self.c, tail)
        self.assertEqual(self.c["no_progress_steps"], 1)

    def test_guidance_survives_recovery_and_has_grace_before_stopping(self):
        observation = self.read()
        for _ in range(32):
            record_progress(self.c, observation)
        self.assertIsNotNone(progress_guidance(self.c))
        self.assertFalse(implementation_stalled(self.c))
        self.c.update(progress_guidance_turn=10, model_turns=12)
        loaded = load_checkpoint(json.loads(json.dumps({"checkpoint": self.c})))
        self.assertFalse(implementation_stalled(loaded))
        loaded["model_turns"] = 13
        self.assertTrue(implementation_stalled(loaded))
        record_progress(loaded, observation, changed=True)
        self.assertIsNone(progress_guidance(loaded))
        self.assertFalse(implementation_stalled(loaded))

    def test_new_reads_do_not_hide_missing_implementation_and_cached_check_is_not_progress(self):
        for i in range(50):
            record_progress(self.c, self.read(path=f"new_{i}.py"))
        self.c.update(progress_guidance_turn=1, model_turns=4)
        self.assertEqual(self.c["no_progress_steps"], 0)
        self.assertTrue(implementation_stalled(self.c))
        check = ToolExecutionResult(
            tool_call_id="c",
            name="run_check",
            ok=True,
            summary="pass",
            data={"check_id": "quick", "source_hash": "checked"},
        )
        record_progress(self.c, check)
        self.assertIsNone(progress_guidance(self.c))
        for _ in range(12):
            record_progress(self.c, check)
        self.assertIsNotNone(progress_guidance(self.c))

    def test_complementary_ranges_survive_alternation_and_history_rollover(self):
        path = "large.py"
        body = "".join(f"LINE_{i:03d} = {i}\n" for i in range(1, 312))
        (self.root / path).write_text(body, encoding="utf-8")
        observations = []
        for start, end in [(1, 120), (240, 311)] * 8:
            result = read_file(self.root, path=path, start_line=start, end_line=end).model_copy(
                update={"arguments": {"path": path, "start_line": start, "end_line": end}}
            )
            record_result(self.c, result)
            observations.append(result)
        context = working_context(self.c, self.root)
        spec = AppSpec.model_validate(valid_spec())
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=WorkItem(id="auth", title="Auth", requirement_ids=[spec.features[0].id]),
            system_design=None,
            engineering_context={"working_memory": context},
            observations=observations,
        )
        native = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        self.assertEqual(len(native), 2)
        self.assertIn("LINE_001", str(native))
        self.assertIn("LINE_311", str(native))
        restored = load_checkpoint(json.loads(json.dumps({"checkpoint": self.c})))
        evidence = working_context(restored, self.root)["source_evidence"]
        self.assertEqual(
            {(e["start_line"], e["end_line"]) for e in evidence}, {(1, 120), (240, 311)}
        )
        self.assertIn("LINE_001", str(evidence))
        self.assertIn("LINE_311", str(evidence))

    def range_read(self, start, end):
        result = read_file(self.root, path="large.py", start_line=start, end_line=end).model_copy(
            update={"arguments": {"path": "large.py", "start_line": start, "end_line": end}}
        )
        record_result(self.c, result)
        return result

    def test_new_source_version_replaces_all_cached_ranges(self):
        (self.root / "large.py").write_text("OLD_HEAD\nmiddle\nOLD_TAIL\n", encoding="utf-8")
        old = [self.range_read(1, 1), self.range_read(3, 3)]
        (self.root / "large.py").write_text("NEW_HEAD\nmiddle\nNEW_TAIL\n", encoding="utf-8")
        # An external change is not silently merged with the prior version.
        new = self.range_read(1, 1)
        context = working_context(self.c, self.root)
        self.assertNotIn("OLD_", str(context["source_evidence"]))
        self.assertNotIn("NEW_TAIL", str(context["source_evidence"]))
        spec = AppSpec.model_validate(valid_spec())
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=WorkItem(id="auth", title="Auth", requirement_ids=[spec.features[0].id]),
            system_design=None,
            engineering_context={"working_memory": context},
            observations=[*old, new],
        )
        native = [m["content"] for m in messages if m["role"] == "tool"]
        self.assertEqual(len(native), 1)
        self.assertNotIn("OLD_", str(native))
        self.assertIn("NEW_HEAD", str(native))

    def test_range_cache_and_model_context_remain_bounded(self):
        body = "".join(f"{i:03d}:" + "x" * 96 + "\n" for i in range(600))
        (self.root / "large.py").write_text(body, encoding="utf-8")
        for start in range(1, 600, 50):
            self.range_read(start, start + 49)
        fragments = self.c["source_evidence"]["large.py"]["fragments"]
        self.assertLessEqual(sum(len(e["content"]) for e in fragments), 24000)
        for i in range(8):
            self.read(f"other{i}.py", "y" * 6000)
        context = working_context(self.c, self.root)
        self.assertLessEqual(sum(len(e["content"]) for e in context["source_evidence"]), 24000)
        self.assertTrue(context["omitted_evidence_ranges"])
        # Version knowledge survives omission from the text budget.
        self.assertIn("large.py", context["source_hashes"])

    def test_legacy_range_migration_recovers_history_and_preserves_stop_state(self):
        (self.root / "large.py").write_text("HEAD\nmiddle\nTAIL\n", encoding="utf-8")
        head, tail = self.range_read(1, 1), self.range_read(3, 3)
        self.c.update(
            schema_version=15,
            outcome="blocked",
            model_turns=72,
            tool_calls=97,
            blocked_reason_code="MODEL_BUDGET_EXHAUSTED",
        )
        # Schema 15 kept only the latest range for each path.
        self.c["source_evidence"] = {"large.py": {**tail.data, "model_turn": 71}}
        self.c["observations"] = [head.model_dump(mode="json"), tail.model_dump(mode="json")]
        loaded = load_checkpoint({"checkpoint": self.c})
        context = working_context(loaded, self.root)
        self.assertEqual(
            {(e["start_line"], e["end_line"]) for e in context["source_evidence"]}, {(1, 1), (3, 3)}
        )
        self.assertEqual(
            (loaded["model_turns"], loaded["tool_calls"], loaded["outcome"]), (72, 97, "blocked")
        )
        self.assertEqual(load_checkpoint({"checkpoint": loaded}), loaded)

    def test_legacy_migration_does_not_revive_reads_before_a_write(self):
        old = self.read("large.py", "OLD_HEAD\nOLD_TAIL\n")
        raw = b"NEW_HEAD\nNEW_TAIL\n"
        (self.root / "large.py").write_bytes(raw)
        self.c.update(
            schema_version=15,
            observations=[old.model_dump(mode="json")],
            observed_hashes={"large.py": sha256_bytes(raw)},
        )
        self.c["source_evidence"] = {
            "large.py": {
                "path": "large.py",
                "content": raw.decode(),
                "content_hash": sha256_bytes(raw),
                "start_line": 1,
                "end_line": 2,
                "line_count": 2,
            }
        }
        loaded = load_checkpoint({"checkpoint": self.c})
        self.assertNotIn("OLD_", str(working_context(loaded, self.root)))
        self.assertIn("NEW_HEAD", str(working_context(loaded, self.root)))

    def test_failed_read_feedback_survives_with_valid_ranges(self):
        success = self.read("large.py", "HEAD\nTAIL\n")
        failure = read_file(self.root, path="large.py", start_line=10, end_line=20)
        spec = AppSpec.model_validate(valid_spec())
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=WorkItem(id="auth", title="Auth", requirement_ids=[spec.features[0].id]),
            system_design=None,
            engineering_context={"working_memory": working_context(self.c, self.root)},
            observations=[success, failure],
        )
        native = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        self.assertEqual(len(native), 2)
        self.assertTrue(any(not e["ok"] for e in native))

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

    def test_legacy_focus_upgrade_keeps_audit_and_does_not_resume_or_reset(self):
        for phase in ["awaiting_focus", "acting", "exhausted"]:
            checkpoint = new_checkpoint()
            checkpoint.update(
                schema_version=14,
                current_work_item_id="auth",
                outcome="blocked",
                blocked_reason_code="NO_SOURCE_PROGRESS",
                model_turns=40,
                tool_calls=90,
            )
            item_memory(checkpoint).update(
                correction={"phase": phase, "read_calls_remaining": 0},
                correction_used=True,
            )
            checkpoint["pending_tool"] = {"name": "update_work_item_focus"}
            item_memory(checkpoint)["recent_actions"] = [
                {"tool": "update_work_item_focus", "summary": "old restriction"},
                {"tool": "model_response", "summary": "old refusal"},
                {"tool": "read_file", "summary": "current source"},
            ]
            loaded = load_checkpoint({"checkpoint": checkpoint})
            context = working_context(loaded, self.root)
            self.assertNotIn("correction", context)
            self.assertEqual([a["tool"] for a in context["recent_actions"]], ["read_file"])
            self.assertNotIn("pending_tool", loaded)
            self.assertEqual(loaded["legacy_pending_tool"]["name"], "update_work_item_focus")
            self.assertEqual(item_memory(loaded)["legacy_correction"]["phase"], phase)
            self.assertEqual(loaded["outcome"], "blocked")
            self.assertEqual((loaded["model_turns"], loaded["tool_calls"]), (40, 90))
            self.assertEqual(load_checkpoint({"checkpoint": loaded}), loaded)

    def test_explicit_stall_retry_keeps_evidence_without_forced_focus(self):
        self.read()
        self.c.update(
            outcome="blocked",
            blocked_reason_code="NO_SOURCE_PROGRESS",
            blocked_reason="Repeated reads",
            model_turns=40,
            tool_calls=90,
        )
        retried = prepare_manual_repair_retry({"checkpoint": self.c})["checkpoint"]
        self.assertEqual((retried["model_turns"], retried["tool_calls"]), (40, 90))
        self.assertIn("user.py", retried["source_evidence"])
        self.assertNotIn("correction", working_context(retried, self.root))
        self.assertTrue(retried["recovery_context"]["historical"])
        self.assertEqual(retried["outcome"], "running")

    def test_work_item_switch_keeps_memory_separate(self):
        self.read()
        self.c["current_work_item_id"] = "jobs"
        self.assertEqual(item_memory(self.c)["inspection_steps"], 0)
        self.assertEqual(item_memory(self.c)["changed_files"], {})

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
        self.assertEqual(loaded["schema_version"], 17)
        self.assertEqual(loaded["outcome"], "blocked")
        self.assertEqual((loaded["model_turns"], loaded["tool_calls"]), (166, 250))


if __name__ == "__main__":
    unittest.main()
