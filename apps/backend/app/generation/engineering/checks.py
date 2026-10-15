"""Apply check evidence to engineering state without database or tool execution."""

from __future__ import annotations

from typing import Any

from app.core.exceptions import ConflictException
from app.generation.delivery import WorkItem
from app.generation.engineering.checkpoint import (
    append_activity,
    block_checkpoint,
    current_item,
    record_check_failure,
)
from app.schemas.agent_action import ToolExecutionResult
from app.tools.check_diagnostics import analyze_check_failure

MAX_IDENTICAL_CHECK_FAILURES = 3
MAX_UNCHANGED_SOURCE_FAILURES = 3


def apply_check_outcome(
    current: dict[str, Any],
    *,
    work_item: WorkItem,
    result: ToolExecutionResult,
    max_repair_rounds: int,
    check_runtime_version: str,
    summary: str | None = None,
) -> None:
    """Advance a checked work item or retain evidence for bounded repair."""
    failure = None if result.ok else analyze_check_failure(result)
    excerpt = "" if failure is None else failure.excerpt
    if result.ok:
        source_hash = result.data.get("source_hash")
        if not isinstance(source_hash, str) or not source_hash:
            raise ConflictException("检查没有返回源码 hash")
        streaks = current.get("failure_streaks")
        if isinstance(streaks, dict):
            streaks.pop(work_item.id, None)
        current_raw = current_item(current["work_items"])
        if current_raw is None or current_raw.get("id") != work_item.id:
            raise ConflictException("当前工作单元已变化")
        current_raw["status"] = "completed"
        current_raw["summary"] = (summary or work_item.title)[:500]
        current["active_file_plan"] = None
        current["active_plan_kind"] = None
        current["deferred_file_plan"] = []
        current["repair_context"] = None
        next_item = current_item(current["work_items"])
        current["current_work_item_id"] = next_item["id"] if next_item else None
        append_activity(
            current,
            name="work_item_complete",
            label="Work item complete",
            detail=work_item.title,
            ok=True,
        )
        if next_item is None:
            if any(item.get("status") != "completed" for item in current["work_items"]):
                block_checkpoint(
                    current,
                    "存在未完成任务但没有可执行的依赖节点",
                    reason_code="INVALID_DELIVERY_PLAN",
                )
            else:
                current["outcome"] = "generated"
                current["completed_source_hash"] = source_hash
    elif result.error_code == "CHECK_ENVIRONMENT_UNAVAILABLE":
        block_checkpoint(current, result.summary, reason_code=result.error_code)
    else:
        assert failure is not None
        fingerprint = failure.fingerprint
        streaks = current.get("failure_streaks")
        streaks = streaks if isinstance(streaks, dict) else {}
        previous = streaks.get(work_item.id)
        previous = previous if isinstance(previous, dict) else {}
        repeated = (
            int(previous.get("count") or 0) + 1 if previous.get("fingerprint") == fingerprint else 1
        )
        source_hash = result.data.get("source_hash")
        stable_source_hash = source_hash if isinstance(source_hash, str) else None
        unchanged_source = (
            int(previous.get("unchanged_source_count") or 0) + 1
            if stable_source_hash is not None and previous.get("source_hash") == stable_source_hash
            else 1
        )
        streaks[work_item.id] = {
            "fingerprint": fingerprint,
            "count": repeated,
            "excerpt": excerpt,
            "source_hash": stable_source_hash,
            "unchanged_source_count": unchanged_source,
        }
        current["failure_streaks"] = streaks
        rounds = current.get("repair_rounds")
        rounds = rounds if isinstance(rounds, dict) else {}
        round_number = int(rounds.get(work_item.id) or 0) + 1
        rounds[work_item.id] = round_number
        current["repair_rounds"] = rounds
        record_check_failure(current, result, round_number=round_number)
        if repeated >= MAX_IDENTICAL_CHECK_FAILURES:
            block_checkpoint(
                current,
                f"相同检查错误已连续出现 {repeated} 次，停止重复修改：{excerpt}",
                reason_code="REPEATED_CHECK_FAILURE",
                check_runtime_version=check_runtime_version,
            )
        elif unchanged_source >= MAX_UNCHANGED_SOURCE_FAILURES:
            block_checkpoint(
                current,
                f"源码连续 {unchanged_source} 次未发生变化，停止无进展修复：{excerpt}",
                reason_code="NO_SOURCE_PROGRESS",
                check_runtime_version=check_runtime_version,
            )
        elif round_number > max_repair_rounds:
            reason = "自动修复轮次已用尽，校验仍未通过"
            if excerpt.strip():
                reason = f"{reason}：{excerpt.strip()}"
            block_checkpoint(
                current,
                reason[:500],
                reason_code="REPAIR_BUDGET_EXHAUSTED",
                check_runtime_version=check_runtime_version,
            )
        else:
            current["active_file_plan"] = None
            current["active_plan_kind"] = None
