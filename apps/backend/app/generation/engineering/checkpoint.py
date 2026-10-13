"""Persisted engineering state, migration and explicit recovery policy.

The JSON shape stays stable across executor refactors. This module performs no
database, model or workspace I/O.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.core.exceptions import ConflictException
from app.generation.engineering.budget import DEFAULT_CALL_BUDGET
from app.schemas.agent_action import ToolExecutionResult
from app.tools.check_diagnostics import CheckFailureAnalysis, analyze_check_failure

CHECKPOINT_KIND = "engineering_checkpoint"
CHECKPOINT_SCHEMA_VERSION = 11
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
        "model_turns": 0,
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


def is_terminal_engineering_failure(checkpoint: dict[str, Any]) -> bool:
    reason = str(checkpoint.get("blocked_reason") or "")
    reason_code = str(checkpoint.get("blocked_reason_code") or "")
    return reason_code in {
        "REPEATED_CHECK_FAILURE",
        "REPAIR_BUDGET_EXHAUSTED",
        "NO_SOURCE_PROGRESS",
    } or ("自动修复轮次已用尽" in reason)


def load_checkpoint(snapshot: dict[str, Any]) -> dict[str, Any]:
    raw = snapshot.get("checkpoint")
    if not isinstance(raw, dict) or raw.get("kind") != CHECKPOINT_KIND:
        return new_checkpoint()

    checkpoint = raw
    previous_mode = checkpoint.get("execution_mode")
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
    return checkpoint


def repair_diagnostics(failure: CheckFailureAnalysis) -> list[dict[str, Any]]:
    root = [item.to_payload() for item in failure.diagnostics if item.kind != "runtime"]
    return root or [item.to_payload() for item in failure.diagnostics]


def seed_repair_from_last_check(checkpoint: dict[str, Any]) -> None:
    """Turn the last failed check into the next repair plan.

    A budget stop used to leave the finished file plan in place, so the next
    attempt only reran the same check. Re-parse that output with the current
    diagnostics so the planner sees the import or schema error, not the
    ``后端启动失败`` wrapper.
    """

    history = checkpoint.get("plan_history")
    if not isinstance(history, list):
        return
    for item in reversed(history):
        if not isinstance(item, dict):
            continue
        check = item.get("check")
        if not isinstance(check, dict) or check.get("ok") is True:
            continue
        try:
            result = ToolExecutionResult.model_validate(check)
        except ValidationError:
            return
        failure = analyze_check_failure(result)
        previous = item.get("plan")
        if not isinstance(previous, dict):
            active = checkpoint.get("active_file_plan")
            previous = active if isinstance(active, dict) else None
        checkpoint["repair_context"] = {
            "round": 1,
            "failed_check": result.model_dump(mode="json"),
            "excerpt": failure.excerpt,
            "diagnostics": repair_diagnostics(failure),
            "previous_plan": previous,
            "modified_files": list(checkpoint.get("modified_files") or []),
        }
        checkpoint["active_file_plan"] = None
        checkpoint["active_plan_kind"] = None
        return


def resume_blocked_checkpoint(checkpoint: dict[str, Any]) -> bool:
    """Make an explicit user recovery attempt runnable from its last real evidence."""

    if checkpoint.get("outcome") != "blocked":
        return False
    terminal_check_failure = is_terminal_engineering_failure(checkpoint)
    if terminal_check_failure:
        return False
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

    current_item_id = str(checkpoint.get("current_work_item_id") or "")
    for key in ("failure_streaks", "repair_rounds"):
        values = checkpoint.get(key)
        if isinstance(values, dict):
            values.pop(current_item_id, None)
    seed_repair_from_last_check(checkpoint)

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
) -> None:
    activity = list(checkpoint.get("activity") or [])
    current = current_item(checkpoint.get("work_items") or [])
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
