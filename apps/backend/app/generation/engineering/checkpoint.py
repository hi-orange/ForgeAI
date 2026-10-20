"""Persisted engineering state, migration and explicit recovery policy.

The JSON shape stays stable across executor refactors. This module performs no
database, model or workspace I/O.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.core.exceptions import ConflictException
from app.generation.delivery import WorkItem
from app.generation.engineering.budget import DEFAULT_CALL_BUDGET
from app.schemas.agent_action import ToolExecutionResult
from app.tools.check_diagnostics import CheckFailureAnalysis, analyze_check_failure

CHECKPOINT_KIND = "engineering_checkpoint"
CHECKPOINT_SCHEMA_VERSION = 17
EXECUTION_MODE = "evidence_tool_loop_v1"


def new_checkpoint() -> dict[str, Any]:
    return {
        "kind": CHECKPOINT_KIND,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "execution_mode": EXECUTION_MODE,
        "work_items": [],
        "current_work_item_id": None,
        "active_file_plan": None,
        "active_plan_kind": None,
        "deferred_file_plan": [],
        "plan_history": [],
        "repair_rounds": {},
        "failure_streaks": {},
        "repair_context": None,
        "diagnostic_state": {},
        "diagnostic_history": [],
        "source_version": 0,
        "work_memory": {},
        "source_evidence": {},
        "model_turns": 0,
        "planning_turns": 0,
        "writer_model_calls": 0,
        "planner_model_calls": 0,
        "tool_calls": 0,
        "modified_files": [],
        "blocked_reason": None,
        "outcome": "running",
        "activity": [],
        "observations": [],
        "observed_hashes": {},
        "no_progress_steps": 0,
    }


def engineering_stop_reason(checkpoint: dict[str, Any]) -> str | None:
    """Read typed reasons; translate old untyped checkpoints at one boundary."""
    code = checkpoint.get("blocked_reason_code")
    if isinstance(code, str) and code:
        return code
    reason = str(checkpoint.get("blocked_reason") or "")
    legacy = {
        "模型调用预算已用尽": "MODEL_BUDGET_EXHAUSTED",
        "工具调用预算已用尽": "TOOL_BUDGET_EXHAUSTED",
    }
    if reason in legacy:
        return legacy[reason]
    if reason.startswith("自动修复轮次已用尽"):
        return "REPAIR_BUDGET_EXHAUSTED"
    return None


def is_terminal_engineering_failure(checkpoint: dict[str, Any]) -> bool:
    reason_code = engineering_stop_reason(checkpoint)
    return reason_code in {
        "REPEATED_CHECK_FAILURE",
        "REPEATED_TOOL_FAILURE",
        "REPAIR_BUDGET_EXHAUSTED",
        "NO_SOURCE_PROGRESS",
        "MODEL_BUDGET_EXHAUSTED",
        "TOOL_BUDGET_EXHAUSTED",
    }


def load_checkpoint(snapshot: dict[str, Any]) -> dict[str, Any]:
    raw = snapshot.get("checkpoint")
    if not isinstance(raw, dict) or raw.get("kind") != CHECKPOINT_KIND:
        return new_checkpoint()

    checkpoint = raw
    previous_mode = checkpoint.get("execution_mode")
    previous_schema = int(checkpoint.get("schema_version") or 0)
    code = engineering_stop_reason(checkpoint)
    if code:
        checkpoint["blocked_reason_code"] = code
    # Historical planning attempts already spent these turns. Recovery must not
    # reopen inspection merely because this field did not exist yet.
    checkpoint.setdefault(
        "planning_turns",
        int(checkpoint.get("model_turns") or 0) if checkpoint.get("needs_delivery_plan") else 0,
    )
    checkpoint["schema_version"] = CHECKPOINT_SCHEMA_VERSION
    for key, default in (
        ("work_items", []),
        ("current_work_item_id", None),
        ("active_file_plan", None),
        ("active_plan_kind", None),
        ("deferred_file_plan", []),
        ("plan_history", []),
        ("repair_rounds", {}),
        ("failure_streaks", {}),
        ("repair_context", None),
        ("diagnostic_state", {}),
        ("diagnostic_history", []),
        ("source_version", 0),
        ("work_memory", {}),
        ("source_evidence", {}),
        ("model_turns", 0),
        ("writer_model_calls", 0),
        ("planner_model_calls", 0),
        ("tool_calls", 0),
        ("modified_files", []),
        ("activity", []),
        ("observations", []),
        ("observed_hashes", {}),
        ("no_progress_steps", 0),
    ):
        checkpoint.setdefault(key, default)

    if previous_mode != EXECUTION_MODE:
        # Retain completed work, evidence and stop-loss budgets. Old file plans are
        # context only: never execute or replay an old pending mutation after upgrade.
        checkpoint["legacy_file_plan"] = checkpoint.get("active_file_plan")
        checkpoint.update(
            execution_mode=EXECUTION_MODE,
            observations=[],
            observed_hashes={},
            active_file_plan=None,
            active_plan_kind=None,
            deferred_file_plan=[],
            migrated_from=previous_mode,
            no_progress_steps=0,
        )
        checkpoint.pop("pending_tool", None)
    if previous_schema < 15:
        # Keep old focus plans as audit data only. Migration never resumes a run.
        for memory in checkpoint["work_memory"].values():
            if "correction" in memory:
                memory["legacy_correction"] = memory.pop("correction")
            memory.pop("correction_used", None)
        pending = checkpoint.get("pending_tool")
        if isinstance(pending, dict) and pending.get("name") == "update_work_item_focus":
            checkpoint["legacy_pending_tool"] = checkpoint.pop("pending_tool")
        for scope, state in checkpoint["diagnostic_state"].items():
            if scope == "tool:model_response:" or scope.startswith("tool:update_work_item_focus:"):
                if state.get("status") == "open":
                    state["status"] = "superseded"
        recovery = checkpoint.get("recovery_context")
        if isinstance(recovery, dict):
            recovery["historical"] = True
    if previous_schema < 16:
        from app.generation.engineering.work_memory import seed_source_evidence

        seed_source_evidence(checkpoint)
    if previous_schema < 17:
        for scope, state in checkpoint["diagnostic_state"].items():
            if scope.startswith("check:") and state.get("status") == "needs_verification":
                state["verification_status"] = "needs_verification"
                # Older invalidation overwrote resolved; only a recorded success
                # can restore it. A later failure replaced this entire record.
                if state.get("verification_source_hash"):
                    state["status"] = "resolved"
        repair = checkpoint.get("repair_context")
        if (
            isinstance(repair, dict)
            and checkpoint["diagnostic_state"]
            .get(f"check:{repair.get('check_id')}", {})
            .get("status")
            == "resolved"
        ):
            checkpoint["repair_context"] = None
        from app.generation.engineering.work_memory import record_progress

        # Recover actual ranges without touching usage, stop reasons or outcomes.
        for row in checkpoint.get("observations", []):
            if row.get("name") == "read_file" and row.get("ok"):
                before = checkpoint.get("no_progress_steps", 0)
                record_progress(checkpoint, ToolExecutionResult.model_validate(row))
                checkpoint["no_progress_steps"] = before
        checkpoint["steps_without_delivery"] = max(
            int(checkpoint.get("steps_without_delivery", 0)),
            int(
                checkpoint.get("work_memory", {})
                .get(str(checkpoint.get("current_work_item_id") or ""), {})
                .get("inspection_steps", 0)
            ),
        )
    return checkpoint


def repair_diagnostics(failure: CheckFailureAnalysis) -> list[dict[str, Any]]:
    root = [item.to_payload() for item in failure.diagnostics if item.kind != "runtime"]
    return root or [item.to_payload() for item in failure.diagnostics]


def record_check_failure(
    checkpoint: dict[str, Any],
    result: ToolExecutionResult,
    *,
    round_number: int = 0,
    check_id: str | None = None,
) -> None:
    """Keep actionable diagnostics even when the observation window rolls over."""
    failure = analyze_check_failure(result)
    from app.tools.check_diagnostics import repair_verification

    checkpoint["repair_context"] = {
        "round": round_number,
        "check_id": check_id or result.data.get("check_id"),
        "failed_check": result.model_dump(mode="json"),
        "excerpt": failure.excerpt,
        "diagnostics": repair_diagnostics(failure),
        "modified_files": list(checkpoint.get("modified_files") or []),
        "verification": repair_verification(result),
    }


def seed_repair_from_last_check(checkpoint: dict[str, Any]) -> None:
    """Turn the last failed check into the next repair plan.

    A budget stop used to leave the finished file plan in place, so the next
    attempt only reran the same check. Re-parse that output with the current
    diagnostics so the planner sees the import or schema error, not the
    ``后端启动失败`` wrapper.
    """

    # Current evidence may come from an explicit run_check after the last
    # completion attempt. Never replace it with an older plan_history entry.
    repair = checkpoint.get("repair_context")
    candidates = [repair.get("failed_check")] if isinstance(repair, dict) else []
    history = checkpoint.get("plan_history")
    if isinstance(history, list):
        candidates.extend(item.get("check") for item in reversed(history) if isinstance(item, dict))
    for check in candidates:
        if not isinstance(check, dict):
            continue
        if check.get("ok") is True:
            return  # A newer passing check supersedes older failures.
        try:
            result = ToolExecutionResult.model_validate(check)
        except ValidationError:
            continue
        record_check_failure(
            checkpoint,
            result,
            check_id=repair.get("check_id") if isinstance(repair, dict) else None,
        )
        checkpoint["active_file_plan"] = None
        checkpoint["active_plan_kind"] = None
        return


def remember_recovery(checkpoint: dict[str, Any]) -> None:
    checkpoint["recovery_context"] = {
        "historical": True,
        "reason": checkpoint.get("blocked_reason"),
        "reason_code": engineering_stop_reason(checkpoint),
        "model_turns": int(checkpoint.get("model_turns") or 0),
        "tool_calls": int(checkpoint.get("tool_calls") or 0),
        "no_progress_steps": int(checkpoint.get("no_progress_steps") or 0),
    }


def resume_blocked_checkpoint(checkpoint: dict[str, Any]) -> bool:
    """Make an explicit user recovery attempt runnable from its last real evidence."""

    if checkpoint.get("outcome") != "blocked":
        return False
    terminal_check_failure = is_terminal_engineering_failure(checkpoint)
    if terminal_check_failure:
        return False
    remember_recovery(checkpoint)
    checkpoint["outcome"] = "running"
    checkpoint["no_progress_steps"] = 0
    checkpoint["blocked_reason"] = None
    checkpoint.pop("blocked_reason_code", None)
    checkpoint.pop("blocked_check_runtime_version", None)
    checkpoint.pop("last_error", None)

    return True


def prepare_manual_repair_retry(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Create one bounded, user-requested retry from terminal check evidence.

    Automatic recovery must preserve the stop-loss. This explicit path keeps the
    frozen inputs and prior activity, but gives the current work item a fresh repair
    budget and forces replanning from the last failed check.
    """

    prepared = json.loads(json.dumps(snapshot))
    checkpoint = load_checkpoint(prepared)
    if checkpoint.get("outcome") != "blocked" or not is_terminal_engineering_failure(checkpoint):
        raise ConflictException("当前工程执行不需要人工重新修复")

    remember_recovery(checkpoint)
    current_item_id = str(checkpoint.get("current_work_item_id") or "")
    for key in ("failure_streaks", "repair_rounds"):
        values = checkpoint.get(key)
        if isinstance(values, dict):
            values.pop(current_item_id, None)
    seed_repair_from_last_check(checkpoint)
    for diagnostic in checkpoint.get("diagnostic_state", {}).values():
        diagnostic["repeated_attempts"] = 0

    call_budget = prepared.get("call_budget")
    call_budget = dict(call_budget) if isinstance(call_budget, dict) else {}
    call_budget["max_model_turns"] = int(checkpoint.get("model_turns") or 0) + int(
        DEFAULT_CALL_BUDGET["max_model_turns"]
    )
    call_budget["max_tool_calls"] = int(checkpoint.get("tool_calls") or 0) + int(
        DEFAULT_CALL_BUDGET["max_tool_calls"]
    )
    call_budget["max_repair_rounds"] = int(DEFAULT_CALL_BUDGET["max_repair_rounds"])
    prepared["call_budget"] = call_budget

    retry_number = int(checkpoint.get("manual_repair_attempts") or 0) + 1
    checkpoint["manual_repair_attempts"] = retry_number
    checkpoint["no_progress_steps"] = 0
    checkpoint["steps_without_delivery"] = 0
    checkpoint["verification_watch"] = {}
    checkpoint.pop("progress_guidance_turn", None)
    checkpoint["outcome"] = "running"
    checkpoint["blocked_reason"] = None
    checkpoint.pop("blocked_reason_code", None)
    checkpoint.pop("blocked_check_runtime_version", None)
    checkpoint.pop("last_error", None)
    append_activity(
        checkpoint,
        name="manual_repair_retry",
        label="Retry repair",
        detail=f"用户要求基于最后一次检查错误重新分析并修复（第 {retry_number} 次）",
        ok=True,
    )
    prepared["checkpoint"] = checkpoint
    return prepared


def current_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    completed = {item["id"] for item in items if item.get("status") == "completed"}
    return next(
        (
            item
            for item in items
            if item.get("status") == "pending" and set(item.get("depends_on", [])) <= completed
        ),
        None,
    )


def append_activity(
    checkpoint: dict[str, Any],
    *,
    name: str,
    label: str,
    detail: str,
    ok: bool,
    work_item: WorkItem | None = None,
) -> None:
    activity = list(checkpoint.get("activity") or [])
    current = (
        work_item.model_dump() if work_item else current_item(checkpoint.get("work_items") or [])
    )
    activity.append(
        {
            "id": f"{name}_{len(activity) + 1}",
            "name": name,
            "label": label,
            "detail": detail[:200],
            "ok": ok,
            "work_item_id": str(current.get("id") or "") if current else "",
            "work_item_title": str(current.get("title") or "") if current else "",
        }
    )
    # Activity is the durable, refresh-safe run history. Trimming the head made the
    # user-visible step counter move backwards during long builds.
    checkpoint["activity"] = activity


def block_checkpoint(
    checkpoint: dict[str, Any],
    reason: str,
    *,
    reason_code: str | None = None,
    check_runtime_version: str | None = None,
) -> None:
    checkpoint["outcome"] = "blocked"
    checkpoint["blocked_reason"] = reason[:500]
    if reason_code:
        checkpoint["blocked_reason_code"] = reason_code
    checkpoint["blocked_executor_runtime_version"] = EXECUTION_MODE
    if check_runtime_version:
        checkpoint["blocked_check_runtime_version"] = check_runtime_version
    else:
        checkpoint.pop("blocked_check_runtime_version", None)
    append_activity(checkpoint, name="error", label="Blocked", detail=reason, ok=False)
