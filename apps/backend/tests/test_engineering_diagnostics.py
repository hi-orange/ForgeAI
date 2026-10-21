"""Correction evidence, without paid model calls or the developer database."""

import json
import unittest
from copy import deepcopy
from pathlib import Path

from test_architect import valid_design
from test_delivery_dependencies import dependency_items, dependency_spec

from app.agents.code_engineer import build_code_engineer_messages
from app.generation.delivery import (
    DeliveryPlanError,
    PlannedWorkItem,
    planning_item,
    replace_delivery_plan,
)
from app.generation.engineering.checkpoint import (
    load_checkpoint,
    new_checkpoint,
    prepare_manual_repair_retry,
    resume_blocked_checkpoint,
)
from app.generation.engineering.diagnostics import (
    MAX_PLAN_REPAIR_ATTEMPT_CHARS,
    MAX_PLAN_REPAIR_ATTEMPTS,
    MAX_PLAN_REPAIR_CHARS,
    MAX_PLAN_REPAIR_HISTORY_CHARS,
    acceptance_feedback,
    active_diagnostics,
    check_verification,
    feedback,
    invalidate_source_evidence,
    planning_repair_context,
    reconcile_protocol_diagnostic,
    record_feedback,
    resolve_protocol_diagnostic,
)
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.schemas.test_report import TestReport
from app.tools.code_engineer import rejected_tool, validate_tool_call


def failed(
    check_id="quick", output="src/router.tsx(12,4): error TS2322: unknown route", source="hash-1"
):
    return ToolExecutionResult(
        tool_call_id="check",
        name="run_check",
        ok=False,
        error_code="CHECK_FAILED",
        summary="check failed",
        arguments={"check_id": check_id},
        data={"check_id": check_id, "source_hash": source, "output": output},
    )


class EngineeringDiagnosticTests(unittest.TestCase):
    def _record_plan(self, checkpoint, *, kind="visual_foundation", provides=False, extra=False):
        spec = dependency_spec()
        items = dependency_items()
        # The identity foundation is linked to login, but does not cover the login feature.
        items[1].kind = "foundation"
        items[1].provides_requirement_ids = ["login"] if provides else []
        items[1].acceptance = ["Session survives reload"]
        items[1].deliverables = ["Session implementation"]
        items[2].kind = kind
        if extra:
            items[0].kind = "foundation"
        checkpoint["model_turns"] += 1
        call = ToolCall(
            id=f"plan_{checkpoint['model_turns']}",
            name="update_delivery_plan",
            arguments={
                "items": [item.model_dump(mode="json") for item in items],
                "reason": "claimed fix",
            },
        )
        with self.assertRaises(DeliveryPlanError) as raised:
            replace_delivery_plan(spec, items, [])
        result = record_feedback(checkpoint, rejected_tool(call, raised.exception)).model_copy(
            update={"model_turn": checkpoint["model_turns"]}
        )
        checkpoint["observations"].append(result.model_dump(mode="json"))
        return result

    def test_plan_repair_tracks_same_requirement_despite_changed_graph_and_other_fixes(self):
        state = new_checkpoint()
        self._record_plan(state, extra=True)
        self._record_plan(state, provides=True)
        before = deepcopy(state)
        context = planning_repair_context(state)
        self.assertEqual(context["mode"], "review_constraints")
        self.assertEqual(
            context["repeated_issues"],
            [{"code": "MISSING_FEATURE_COVERAGE", "requirement_id": "login", "occurrences": 2}],
        )
        self.assertEqual(context["regressed_issues"], [])
        self.assertEqual(
            context["recent_attempts"][0]["relevant_items"][1]["provides_requirement_ids"], []
        )
        self.assertEqual(
            context["recent_attempts"][1]["relevant_items"][0]["provides_requirement_ids"],
            ["login"],
        )
        self.assertEqual(state, before)
        self.assertEqual(state["diagnostic_state"]["plan"]["repeated_attempts"], 1)

    def test_plan_repair_detects_returning_error_and_survives_checkpoint_reload(self):
        state = new_checkpoint()
        self._record_plan(state)
        # Changing the theme into a feature supplies login coverage but loses the foundation.
        self._record_plan(state, kind="feature")
        latest = self._record_plan(state, provides=True)
        state.update(outcome="blocked", blocked_reason_code="NO_SOURCE_PROGRESS")
        loaded = load_checkpoint({"checkpoint": json.loads(json.dumps(state))})
        context = planning_repair_context(loaded)
        self.assertEqual(context["mode"], "review_constraints")
        self.assertEqual(context["regressed_issues"][0]["requirement_id"], "login")
        self.assertEqual(context["regressed_issues"][0]["occurrences"], 2)
        self.assertEqual(loaded["outcome"], "blocked")
        self.assertEqual(loaded["model_turns"], 3)
        spec = dependency_spec()
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            system_design=None,
            engineering_context={"planning_repair": context},
            observations=[
                ToolExecutionResult.model_validate(row) for row in loaded["observations"]
            ],
        )
        tool_messages = [message for message in messages if message["role"] == "tool"]
        self.assertEqual(
            [message["tool_call_id"] for message in tool_messages], [latest.tool_call_id]
        )
        model_context = json.loads(messages[-1]["content"])["engineering_context"][
            "planning_repair"
        ]
        self.assertEqual(len(model_context["recent_attempts"]), 3)
        self.assertTrue(model_context["instruction"])
        self.assertNotIn("instruction", context)

    def test_accepted_plan_clears_repair_context_and_starts_new_failure_window(self):
        state = new_checkpoint()
        self._record_plan(state)
        self._record_plan(state, provides=True)
        accepted = ToolExecutionResult(
            tool_call_id="accepted",
            name="update_delivery_plan",
            ok=True,
            summary="accepted",
            arguments={"suggested_plan_hash": "accepted-hash", "reason": "adopt suggestion"},
        )
        record_feedback(state, accepted)
        self.assertIsNone(planning_repair_context(state))
        self.assertEqual(state["planning_repair_history"]["attempts"], [])
        state["observations"].append(accepted.model_dump(mode="json"))
        self._record_plan(state)
        context = planning_repair_context(state)
        self.assertEqual(context["mode"], "repair")
        self.assertEqual(context["repeated_issues"], [])
        self.assertEqual(len(context["recent_attempts"]), 1)
        self.assertEqual(len(state["observations"]), 4)

    def test_plan_repair_history_survives_unrelated_observation_eviction(self):
        state = new_checkpoint()
        self._record_plan(state)
        self._record_plan(state, kind="feature")
        for index in range(24):
            read = ToolExecutionResult(
                tool_call_id=f"read-{index}", name="read_file", ok=True, summary="read"
            )
            record_feedback(state, read)
            state["observations"] = [*state["observations"], read.model_dump(mode="json")][-24:]
        self.assertFalse(
            any(row["name"] == "update_delivery_plan" for row in state["observations"])
        )
        loaded = load_checkpoint({"checkpoint": json.loads(json.dumps(state))})
        before = deepcopy(loaded)
        self.assertEqual(
            [
                attempt["model_turn"]
                for attempt in planning_repair_context(loaded)["recent_attempts"]
            ],
            [1, 2],
        )
        self.assertEqual(loaded, before)
        self._record_plan(loaded, provides=True)
        context = planning_repair_context(loaded)
        self.assertEqual(context["mode"], "review_constraints")
        self.assertEqual(context["regressed_issues"][0]["requirement_id"], "login")
        self.assertEqual(context["regressed_issues"][0]["occurrences"], 2)
        self.assertEqual(
            [attempt["model_turn"] for attempt in context["recent_attempts"]], [1, 2, 3]
        )

    def test_legacy_plan_attempts_are_restored_once_without_changing_stop_loss(self):
        state = new_checkpoint()
        self._record_plan(state)
        self._record_plan(state, kind="feature")
        self._record_plan(state)
        state.pop("planning_repair_history")
        # The audit log deduplicates the two identical failures, unlike observations.
        self.assertEqual(len(state["diagnostic_history"]), 2)
        state.update(
            outcome="blocked",
            blocked_reason_code="NO_SOURCE_PROGRESS",
            no_progress_steps=12,
            planning_turns=8,
            tool_calls=27,
        )
        loaded = load_checkpoint({"checkpoint": json.loads(json.dumps(state))})
        before = deepcopy(loaded)
        context = planning_repair_context(loaded)
        self.assertTrue(context["legacy_history_partial"])
        self.assertEqual(context["regressed_issues"][0]["occurrences"], 2)
        self.assertEqual(loaded, before)
        self._record_plan(loaded, provides=True)
        context = planning_repair_context(loaded)
        self.assertEqual(
            [attempt["model_turn"] for attempt in context["recent_attempts"]], [1, 2, 3, 4]
        )
        self.assertEqual(context["repeated_issues"][0]["occurrences"], 3)
        self.assertEqual(loaded["outcome"], "blocked")
        self.assertEqual(loaded["blocked_reason_code"], "NO_SOURCE_PROGRESS")
        self.assertEqual(loaded["no_progress_steps"], 12)
        self.assertEqual(loaded["planning_turns"], 8)
        self.assertEqual(loaded["tool_calls"], 27)

    def test_legacy_plan_attempts_stop_at_last_accepted_plan(self):
        state = new_checkpoint()
        self._record_plan(state)
        accepted = ToolExecutionResult(
            tool_call_id="accepted", name="update_delivery_plan", ok=True, summary="accepted"
        )
        record_feedback(state, accepted)
        state["observations"].append(accepted.model_dump(mode="json"))
        self._record_plan(state, kind="feature")
        state.pop("planning_repair_history")
        context = planning_repair_context(state)
        self.assertFalse(context["legacy_history_partial"])
        self.assertEqual(len(context["recent_attempts"]), 1)
        self._record_plan(state)
        context = planning_repair_context(state)
        self.assertEqual([attempt["model_turn"] for attempt in context["recent_attempts"]], [2, 3])
        self.assertEqual(context["repeated_issues"], [])

    def test_plan_repair_projection_is_bounded_and_tolerates_schema_rejections(self):
        state = new_checkpoint()
        for _ in range(10):
            self._record_plan(state)
        malformed = ToolCall(
            id="bad",
            name="update_delivery_plan",
            arguments={
                "items": [{"id": [], "kind": [], "title": "x" * 100_000}],
                "reason": "not evidence",
            },
        )
        with self.assertRaises(ValueError) as raised:
            validate_tool_call(malformed)
        result = record_feedback(state, rejected_tool(malformed, raised.exception))
        state["observations"].append(result.model_dump(mode="json"))
        context = planning_repair_context(state)
        self.assertGreaterEqual(context["omitted_attempts"], 5)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), MAX_PLAN_REPAIR_CHARS)
        self.assertNotIn("not evidence", json.dumps(context))
        self.assertEqual(len(state["observations"]), 11)
        history = state["planning_repair_history"]
        self.assertLessEqual(len(history["attempts"]), MAX_PLAN_REPAIR_ATTEMPTS)
        self.assertLessEqual(
            len(json.dumps(history, ensure_ascii=False)), MAX_PLAN_REPAIR_HISTORY_CHARS
        )
        self.assertNotIn("not evidence", json.dumps(history))
        self.assertNotIn("x" * 100_000, json.dumps(history))
        for attempt in history["attempts"]:
            self.assertLessEqual(
                len(json.dumps(attempt, ensure_ascii=False)), MAX_PLAN_REPAIR_ATTEMPT_CHARS
            )

    def test_plan_repair_bounds_invalid_field_locations_without_merging_distinct_errors(self):
        state = new_checkpoint()
        prefix = "invalid_" + "x" * 20_000
        for suffix in ("first", "second", "first"):
            call = ToolCall(
                id=suffix,
                name="update_delivery_plan",
                arguments={"items": [], "reason": "invalid", prefix + suffix: True},
            )
            with self.assertRaises(ValueError) as raised:
                validate_tool_call(call)
            result = record_feedback(state, rejected_tool(call, raised.exception))
            state["observations"].append(result.model_dump(mode="json"))
        context = planning_repair_context(state)
        encoded = json.dumps(context, ensure_ascii=False)
        self.assertLessEqual(len(encoded), MAX_PLAN_REPAIR_CHARS)
        self.assertNotIn(prefix, encoded)
        repeated = [issue for issue in context["repeated_issues"] if issue.get("identity_hash")]
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0]["occurrences"], 2)

    def test_plan_attempt_storage_bounds_many_large_schema_errors(self):
        state = new_checkpoint()
        arguments = {
            "items": [],
            "reason": "invalid",
            **{f"unexpected_{index}_" + "x" * 20_000: True for index in range(30)},
        }
        call = ToolCall(id="large-invalid", name="update_delivery_plan", arguments=arguments)
        with self.assertRaises(ValueError) as raised:
            validate_tool_call(call)
        error = rejected_tool(call, raised.exception)
        for turn in range(1, 10):
            state["model_turns"] = turn
            record_feedback(state, error)
        history = state["planning_repair_history"]
        self.assertTrue(history["attempts"])
        self.assertEqual(history["attempts"][-1]["model_turn"], 9)
        self.assertEqual(history["omitted_attempts"] + len(history["attempts"]), 9)
        self.assertGreater(history["attempts"][-1]["omitted_problems"], 0)
        self.assertLessEqual(
            len(json.dumps(history, ensure_ascii=False)), MAX_PLAN_REPAIR_HISTORY_CHARS
        )
        for attempt in history["attempts"]:
            self.assertLessEqual(
                len(json.dumps(attempt, ensure_ascii=False)), MAX_PLAN_REPAIR_ATTEMPT_CHARS
            )
        self.assertLessEqual(
            len(json.dumps(planning_repair_context(state), ensure_ascii=False)),
            MAX_PLAN_REPAIR_CHARS,
        )

    def test_protocol_lifetime_tracks_capabilities_not_source_or_other_success(self):
        checkpoint = new_checkpoint()
        available = ["read_file", "write_file"]
        error = rejected_tool(
            ToolCall(id="protocol", name="model_response"), ValueError("unavailable")
        )
        error = error.model_copy(update={"data": {**error.data, "available_tools": available}})
        record_feedback(checkpoint, error)
        record_feedback(checkpoint, failed())
        record_feedback(
            checkpoint,
            rejected_tool(
                ToolCall(id="write", name="write_file", arguments={"path": "private.py"}),
                ValueError("permission denied"),
            ),
        )
        invalidate_source_evidence(checkpoint)
        read = ToolExecutionResult(tool_call_id="r", name="read_file", ok=True, summary="read")
        record_feedback(checkpoint, read)
        reconcile_protocol_diagnostic(checkpoint, available)
        self.assertEqual(len(active_diagnostics(checkpoint)), 3)
        resolve_protocol_diagnostic(checkpoint)
        self.assertEqual(
            {d["scope"] for d in active_diagnostics(checkpoint)},
            {"check:quick", "tool:write_file:private.py"},
        )
        record_feedback(checkpoint, error)
        reconcile_protocol_diagnostic(checkpoint, [*available, "run_check"])
        self.assertEqual(len(active_diagnostics(checkpoint)), 2)
        self.assertEqual(
            checkpoint["diagnostic_state"]["tool:model_response:"]["status"], "superseded"
        )
        self.assertTrue(checkpoint["diagnostic_history"])

    def test_protocol_repetition_is_not_reset_by_source_edits(self):
        checkpoint = new_checkpoint()
        error = rejected_tool(
            ToolCall(id="protocol", name="model_response"), ValueError("bad batch")
        )
        error = error.model_copy(update={"data": {**error.data, "available_tools": ["read_file"]}})
        record_feedback(checkpoint, error)
        invalidate_source_evidence(checkpoint)
        repeated = record_feedback(checkpoint, error)
        self.assertEqual(repeated.data["correction"]["repeated_attempts"], 2)
        resolve_protocol_diagnostic(checkpoint)
        fresh = record_feedback(checkpoint, error)
        self.assertEqual(fresh.data["correction"]["repeated_attempts"], 1)

    def test_legacy_recovery_does_not_replay_retired_or_pseudo_tools(self):
        checkpoint = new_checkpoint()
        error = rejected_tool(
            ToolCall(id="protocol", name="model_response"), ValueError("only focus allowed")
        )
        record_feedback(checkpoint, error)
        record_feedback(checkpoint, failed())
        record_feedback(
            checkpoint,
            rejected_tool(
                ToolCall(id="focus", name="update_work_item_focus"),
                ValueError("wrong work item"),
            ),
        )
        checkpoint.update(
            schema_version=14,
            outcome="blocked",
            blocked_reason="only focus allowed",
            recovery_context={"reason": "only focus allowed"},
        )
        loaded = load_checkpoint({"checkpoint": checkpoint})
        self.assertEqual(loaded["outcome"], "blocked")
        self.assertTrue(resume_blocked_checkpoint(loaded))
        self.assertTrue(loaded["recovery_context"]["historical"])
        self.assertEqual([d["scope"] for d in active_diagnostics(loaded)], ["check:quick"])
        native = ToolExecutionResult(
            tool_call_id="r", name="read_file", ok=True, summary="current source"
        )
        focus = ToolExecutionResult(
            tool_call_id="f", name="update_work_item_focus", ok=True, summary="old plan"
        )
        spec = dependency_spec()
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            system_design=None,
            engineering_context={"active_diagnostics": active_diagnostics(loaded)},
            observations=[error, focus, native],
        )
        calls = [c["function"]["name"] for m in messages for c in m.get("tool_calls", [])]
        self.assertEqual(calls, ["read_file"])
        self.assertEqual(len([m for m in messages if m["role"] == "tool"]), 1)

    def test_real_rejected_plan_reports_both_conflicts_and_repaired_plan_passes(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/engineering_feedback.json").read_text(
                encoding="utf-8"
            )
        )
        spec = AppSpec.model_validate(fixture["spec"])
        design = SystemDesign.model_validate(
            {**valid_design(), "implementation_dependencies": fixture["dependencies"]}
        )
        original = [PlannedWorkItem.model_validate(i) for i in fixture["original"]]
        with self.assertRaises(DeliveryPlanError) as raised:
            replace_delivery_plan(spec, original, [], design)
        call = ToolCall(
            id="plan",
            name="update_delivery_plan",
            arguments={"items": fixture["original"], "reason": "plan"},
        )
        result = rejected_tool(call, raised.exception)
        issues = result.data["diagnostic"]["issues"]
        self.assertEqual(
            {i["work_item_id"] for i in issues}, {"backend_jobs_public", "backend_applications"}
        )
        self.assertTrue(all(i["prerequisite_in_same_item"] for i in issues))
        self.assertTrue(all(i["suggested_action"] for i in issues))
        repaired = [PlannedWorkItem.model_validate(i) for i in fixture["repaired"]]
        self.assertEqual(len(replace_delivery_plan(spec, repaired, [], design)), 17)
        # Removing a real prerequisite is still rejected, even after the repair.
        next(i for i in repaired if i.id == "backend_job_browse").depends_on = [
            "backend_foundation"
        ]
        with self.assertRaises(DeliveryPlanError):
            replace_delivery_plan(spec, repaired, [], design)

    def test_structural_errors_locate_each_invalid_work_item(self):
        items = dependency_items()
        items[0].depends_on = ["missing"]
        items[1].requirement_ids = ["unapproved"]
        with self.assertRaises(DeliveryPlanError) as raised:
            replace_delivery_plan(dependency_spec(), items, [])
        self.assertEqual(
            {i["work_item_id"] for i in raised.exception.diagnostic.issues}, {"projects", "auth"}
        )

    def test_schema_rejection_has_field_location_without_input_echo(self):
        call = ToolCall(id="invalid", name="read_file", arguments={"path": "x", "start_line": 0})
        try:
            validate_tool_call(call)
        except ValueError as exc:
            result = rejected_tool(call, exc)
        self.assertEqual(result.data["diagnostic"]["issues"][0]["loc"], ["start_line"])
        self.assertNotIn("input", result.data["diagnostic"]["issues"][0])

    def test_compiler_api_and_migration_evidence_use_same_envelope(self):
        compiler = feedback(failed()).data["diagnostic"]
        self.assertEqual(compiler["issues"][0]["path"], "src/router.tsx")
        self.assertEqual(compiler["issues"][0]["line"], 12)
        api = feedback(
            failed(
                "all", "RuntimeError: API 联调失败 create：期望 HTTP 201，实际 422；invalid body"
            )
        ).data["diagnostic"]
        self.assertEqual(api["category"], "api")
        self.assertEqual((api["issues"][0]["expected"], api["issues"][0]["actual"]), ("201", "422"))
        migration = feedback(
            failed("database", "FAILED: New upgrade operations detected: missing unique index")
        ).data["diagnostic"]
        self.assertEqual(migration["category"], "database")
        self.assertIn("unique index", migration["issues"][0]["raw"])
        self.assertEqual(migration["source_hash"], "hash-1")

    def test_environment_and_stale_source_do_not_invent_code_causes(self):
        for code, disposition in [
            ("CHECK_ENVIRONMENT_UNAVAILABLE", "restore_environment"),
            ("CHECK_TIMEOUT", "restore_environment"),
            ("SOURCE_CHANGED", "refresh_evidence"),
        ]:
            result = failed().model_copy(update={"error_code": code, "data": {}, "summary": code})
            diagnostic = feedback(result).data["diagnostic"]
            self.assertEqual(diagnostic["disposition"], disposition)
            self.assertEqual(diagnostic["issues"][0]["cause"], "unknown")

    def test_same_failure_keeps_raw_evidence_once_and_counts_repeats(self):
        state = new_checkpoint()
        original = failed()
        before = original.model_dump(mode="json")
        for _ in range(3):
            result = record_feedback(state, original)
        self.assertEqual(result.data["correction"]["repeated_attempts"], 3)
        self.assertEqual(len(state["diagnostic_history"]), 1)
        self.assertEqual(
            state["diagnostic_history"][0]["result"]["data"]["output"], original.data["output"]
        )
        self.assertEqual(original.model_dump(mode="json"), before)
        changed = record_feedback(state, failed(source="hash-2"))
        self.assertEqual(changed.data["correction"]["repeated_attempts"], 1)

    def test_changed_diagnostic_replaces_current_error_but_retains_history(self):
        state = new_checkpoint()
        record_feedback(state, failed())
        record_feedback(state, failed(output="src/model.ts(7,1): error TS2339: missing field"))
        diagnostic = active_diagnostics(state)[0]["diagnostic"]
        self.assertEqual(diagnostic["issues"][0]["code"], "TS2339")
        self.assertEqual(len(state["diagnostic_history"]), 2)

    def test_only_same_check_can_resolve_error_and_source_write_requires_recheck(self):
        state = new_checkpoint()
        record_feedback(state, failed("database"))
        passed = failed("quick").model_copy(update={"ok": True, "error_code": None})
        record_feedback(state, passed)
        self.assertEqual(state["diagnostic_state"]["check:database"]["status"], "open")
        record_feedback(
            state, failed("database").model_copy(update={"ok": True, "error_code": None})
        )
        self.assertEqual(active_diagnostics(state), [])
        state["check_cache"] = {"database": {"data": {"source_hash": "hash-1"}}}
        invalidate_source_evidence(state)
        self.assertEqual(state["check_cache"], {})
        self.assertEqual(active_diagnostics(state), [])
        self.assertTrue(all(row["last_outcome"] == "passed" for row in check_verification(state)))
        self.assertTrue(
            all(
                row["verification_status"] == "needs_verification"
                for row in check_verification(state)
            )
        )
        record_feedback(state, passed.model_copy(update={"data": {}}), check_id="database")
        self.assertEqual(active_diagnostics(state), [])
        self.assertTrue(all(row["last_outcome"] == "passed" for row in check_verification(state)))
        self.assertTrue(
            all(
                row["verification_status"] == "needs_verification"
                for row in check_verification(state)
            )
        )

    def test_new_failure_reopens_only_from_actual_check_and_legacy_pass_stays_resolved(self):
        state = new_checkpoint()
        record_feedback(state, failed("frontend"))
        record_feedback(state, failed("frontend").model_copy(update={"ok": True}))
        # Exact shape produced by schema16 after unrelated source edits.
        state.update(
            schema_version=16,
            outcome="blocked",
            model_turns=79,
            tool_calls=120,
            blocked_reason_code="TOOL_BUDGET_EXHAUSTED",
        )
        state["diagnostic_state"]["check:frontend"]["status"] = "needs_verification"
        restored = load_checkpoint({"checkpoint": json.loads(json.dumps(state))})
        self.assertEqual(active_diagnostics(restored), [])
        self.assertEqual(restored["outcome"], "blocked")
        self.assertEqual((restored["model_turns"], restored["tool_calls"]), (79, 120))
        record_feedback(restored, failed("frontend", source="hash-2"))
        self.assertEqual(active_diagnostics(restored)[0]["diagnostic"]["source_hash"], "hash-2")
        self.assertFalse(active_diagnostics(restored)[0]["historical"])
        invalidate_source_evidence(restored)
        self.assertTrue(active_diagnostics(restored)[0]["historical"])

    def test_solved_error_does_not_return_from_rolling_history_or_repair_context(self):
        state = new_checkpoint()
        error = failed("frontend")
        record_feedback(state, error)
        record_feedback(state, error.model_copy(update={"ok": True}))
        invalidate_source_evidence(state)
        spec = dependency_spec()
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            system_design=None,
            observations=[error],
            engineering_context={
                "check_verification": check_verification(state),
                "repair_context": {"check_id": "frontend", "excerpt": "obsolete TS error"},
            },
        )
        self.assertFalse(any(m["role"] == "tool" for m in messages))
        context = json.loads(messages[-1]["content"])["engineering_context"]
        self.assertIsNone(context["repair_context"])
        self.assertEqual(
            context["check_verification"][0]["verification_status"], "needs_verification"
        )

    def test_manual_recovery_preserves_diagnostic_and_usage_but_grants_bounded_attempt(self):
        state = new_checkpoint()
        for _ in range(3):
            record_feedback(state, failed())
        state.update(
            outcome="blocked",
            blocked_reason_code="REPEATED_TOOL_FAILURE",
            blocked_reason="same failure",
            model_turns=7,
        )
        state = load_checkpoint({"checkpoint": json.loads(json.dumps(state))})
        self.assertFalse(resume_blocked_checkpoint(state))
        retry = prepare_manual_repair_retry({"checkpoint": state})["checkpoint"]
        self.assertEqual(retry["model_turns"], 7)
        self.assertEqual(len(retry["diagnostic_history"]), 1)
        self.assertEqual(retry["diagnostic_state"]["check:quick"]["repeated_attempts"], 0)
        self.assertEqual(state["diagnostic_state"]["check:quick"]["repeated_attempts"], 3)

    def test_old_checkpoint_gets_additive_diagnostic_fields(self):
        state = new_checkpoint()
        for key in ("diagnostic_state", "diagnostic_history", "source_version"):
            state.pop(key)
        state.update(schema_version=12, model_turns=23)
        loaded = load_checkpoint({"checkpoint": state})
        self.assertEqual(loaded["model_turns"], 23)
        self.assertEqual(loaded["diagnostic_state"], {})

    def test_model_history_keeps_latest_plan_pair_without_destroying_audit(self):
        spec = dependency_spec()
        items = [i.model_dump(mode="json") for i in dependency_items()]
        observation = ToolExecutionResult(
            tool_call_id="old",
            name="update_delivery_plan",
            ok=False,
            summary="old error",
            arguments={"items": items, "reason": "old"},
        )
        latest = observation.model_copy(update={"tool_call_id": "new", "summary": "new diagnosis"})
        observations = [observation, latest]
        before = deepcopy(observations)
        context = {
            "delivery_plan": [{"id": str(i)} for i in range(50)],
            "repair_context": {"failed_check": {"raw": "large"}, "excerpt": "current"},
        }
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            engineering_context=context,
            observations=observations,
            system_design=None,
        )
        tools = [m for m in messages if m["role"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["tool_call_id"], "new")
        self.assertEqual(json.loads(tools[0]["content"])["arguments"], {})
        self.assertEqual(
            len(json.loads(messages[-1]["content"])["engineering_context"]["delivery_plan"]), 50
        )
        self.assertEqual(observations, before)
        self.assertIn("failed_check", context["repair_context"])

    def test_qa_evidence_keeps_frozen_identity_and_cannot_be_resolved_by_smoke(self):
        report = TestReport.model_validate(
            {
                "code_item_id": "code",
                "source_hash": "a" * 64,
                "requirement_results": [
                    {
                        "requirement_id": "private",
                        "status": "failed",
                        "evidence": "Unauthorized read",
                    }
                ],
                "check_results": [
                    {"check_id": "all", "status": "passed", "evidence": "Smoke passed"}
                ],
                "quality_conclusion": "failed",
                "summary": "Access control failed",
            }
        )
        before = report.model_dump(mode="json")
        diagnostic = acceptance_feedback(report)
        state = new_checkpoint()
        state["diagnostic_state"]["acceptance"] = {
            "status": "open",
            "diagnostic": diagnostic.model_dump(mode="json"),
        }
        record_feedback(state, failed("all").model_copy(update={"ok": True, "error_code": None}))
        self.assertEqual(active_diagnostics(state)[0]["scope"], "acceptance")
        self.assertEqual(diagnostic.source_hash, "a" * 64)
        self.assertEqual(report.model_dump(mode="json"), before)

    def test_malformed_plan_arguments_become_feedback_instead_of_executor_crash(self):
        for items in (["not-an-item"], {"id": "not-a-list"}, None):
            with self.subTest(items=items):
                call = ToolCall(id="bad", name="update_delivery_plan", arguments={"items": items})
                with self.assertRaises(ValueError) as raised:
                    validate_tool_call(call)
                result = record_feedback(new_checkpoint(), rejected_tool(call, raised.exception))
                self.assertFalse(result.ok)
                self.assertTrue(result.data["diagnostic"]["issues"])

    def test_latest_large_pending_plan_is_not_silently_truncated(self):
        spec = dependency_spec()
        items = [
            {"id": f"item_{i}", "requirement_ids": ["login"], "title": "Slice"} for i in range(50)
        ]
        observation = ToolExecutionResult(
            tool_call_id="plan",
            name="update_delivery_plan",
            ok=False,
            summary="missing dependency",
            arguments={"items": items, "reason": "complete graph"},
        )
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            engineering_context={},
            observations=[observation],
            system_design=None,
        )
        call = next(m for m in messages if m["role"] == "assistant")["tool_calls"][0]
        self.assertEqual(len(json.loads(call["function"]["arguments"])["items"]), 50)
