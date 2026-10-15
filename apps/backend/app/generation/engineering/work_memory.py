"""Source-bound working evidence and bounded recovery from implementation stalls.

Model-authored focus is a hypothesis, never completion or permission to write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.exceptions import AppException, ConflictException
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.tools.paths import MAX_FILE_BYTES, safe_path_under_root, sha256_bytes

READ_TOOLS = frozenset({"read_file", "list_files", "search_code"})
FOCUS_TOOL = "update_work_item_focus"
MAX_INSPECTIONS = 12
MAX_RECOVERY_READS = 4
MAX_RECOVERY_ACTIONS = 8
MAX_EVIDENCE_FILES = 12
MAX_EVIDENCE_CHARS = 6000
MAX_CONTEXT_CHARS = 24000


def item_memory(checkpoint: dict[str, Any]) -> dict[str, Any]:
    key = str(checkpoint.get("current_work_item_id") or "")
    return checkpoint.setdefault("work_memory", {}).setdefault(
        key,
        {
            "changed_files": {},
            "recent_actions": [],
            "inspection_steps": 0,
            "correction_used": False,
            "correction": None,
        },
    )


def correction(checkpoint: dict[str, Any]) -> dict[str, Any]:
    key = str(checkpoint.get("current_work_item_id") or "")
    return checkpoint.get("work_memory", {}).get(key, {}).get("correction") or {}


def request_correction(checkpoint: dict[str, Any], reason: str) -> None:
    memory = item_memory(checkpoint)
    memory["correction_used"] = True
    memory["correction"] = {
        "phase": "awaiting_focus",
        "reason": reason,
        "requested_at_turn": checkpoint.get("model_turns", 0),
        "read_calls_remaining": MAX_RECOVERY_READS,
        "actions_remaining": MAX_RECOVERY_ACTIONS,
    }


def prepare_stall_retry(checkpoint: dict[str, Any]) -> None:
    if not checkpoint.get("needs_delivery_plan"):
        request_correction(
            checkpoint, "上次执行因无进展停止；先根据已有证据调整下一步，不能原样重试"
        )


def tool_names(checkpoint: dict[str, Any], available: set[str]) -> set[str]:
    state = correction(checkpoint)
    if state.get("phase") == "awaiting_focus":
        return available & {FOCUS_TOOL, "report_blocked"}
    names = available - {FOCUS_TOOL}
    if state.get("phase") == "acting":
        names -= {"list_files", "search_code", "update_delivery_plan"}
        if state["read_calls_remaining"] <= 0:
            names.discard("read_file")
    return names


def validate_focus(checkpoint: dict[str, Any], args: dict[str, Any], root: Path) -> None:
    if correction(checkpoint).get("phase") != "awaiting_focus":
        raise ConflictException("当前没有待处理的停滞纠偏")
    if args["work_item_id"] != checkpoint.get("current_work_item_id"):
        raise ConflictException("只能调整当前工作项的下一步")
    for path in [*args["target_paths"], *args["read_paths"]]:
        canonical = safe_path_under_root(root, path, allow_create=True).relative_to(root.resolve())
        if canonical.as_posix() != path:
            raise ConflictException("纠偏路径必须是规范的工作区相对路径")


def accept_focus(checkpoint: dict[str, Any], args: dict[str, Any]) -> None:
    state = correction(checkpoint)
    state.update(phase="acting", next_action=args)
    # The current correction is durable in work_memory; prior corrections remain
    # in workflow activity. This is not progress and never resets stop-loss usage.


def validate_action(checkpoint: dict[str, Any], call: ToolCall, available: set[str]) -> None:
    if call.name not in tool_names(checkpoint, available):
        raise ConflictException("当前纠偏阶段不开放该工具，请依据 working_memory 选择下一步")
    state = correction(checkpoint)
    if state.get("phase") == "acting" and call.name == "read_file":
        allowed = state["next_action"]["read_paths"]
        if call.arguments["path"] not in allowed:
            raise ConflictException(
                "纠偏期间只允许读取已声明的必要 read_paths；缺少条件可 report_blocked"
            )


def _cache_evidence(checkpoint: dict[str, Any], data: dict[str, Any]) -> None:
    path = data.get("path")
    if not isinstance(path, str) or not isinstance(data.get("content"), str):
        return
    cache = checkpoint.setdefault("source_evidence", {})
    old = cache.get(path)
    # Re-reading identical ranges must not evict another useful file.
    if old and all(old.get(k) == data.get(k) for k in ("content_hash", "start_line", "end_line")):
        return
    body = data["content"]
    cache[path] = {
        **{
            k: data.get(k) for k in ("path", "content_hash", "start_line", "end_line", "line_count")
        },
        "content": body[:MAX_EVIDENCE_CHARS],
        "content_truncated": len(body) > MAX_EVIDENCE_CHARS,
        "work_item_id": checkpoint.get("current_work_item_id"),
        "model_turn": checkpoint.get("model_turns", 0),
    }
    # Prefer the current work item's changed files and declared correction targets.
    memory = item_memory(checkpoint)
    focus = correction(checkpoint).get("next_action", {})
    pinned = set(memory["changed_files"]) | set(focus.get("target_paths", [])) | {path}
    while len(cache) > MAX_EVIDENCE_FILES:
        victim = min(cache, key=lambda p: (p in pinned, cache[p].get("model_turn", 0)))
        del cache[victim]


def seed_source_evidence(checkpoint: dict[str, Any]) -> None:
    """Recover bounded read evidence from older checkpoints without replaying actions."""
    for row in checkpoint.get("observations", []):
        if row.get("ok") and row.get("name") == "read_file":
            _cache_evidence(checkpoint, row.get("data", {}))


def record_result(
    checkpoint: dict[str, Any],
    result: ToolExecutionResult,
    *,
    updates: dict[str, bytes] | None = None,
) -> str | None:
    """Record facts; return requested/exhausted when a stall needs orchestration."""
    if result.ok and result.name == "read_file":
        _cache_evidence(checkpoint, result.data)
    if checkpoint.get("needs_delivery_plan") or result.name == "update_delivery_plan":
        return None
    memory = item_memory(checkpoint)
    memory["recent_actions"] = [
        *memory["recent_actions"],
        {
            "tool": result.name,
            "path": result.arguments.get("path"),
            "ok": result.ok,
            "summary": result.summary[:500],
        },
    ][-12:]
    changed = updates if result.ok else None
    for path, raw in (changed or {}).items():
        digest = sha256_bytes(raw)
        memory["changed_files"][path] = digest
        checkpoint.setdefault("source_evidence", {}).pop(path, None)
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        _cache_evidence(
            checkpoint,
            {
                "path": path,
                "content_hash": digest,
                "content": body,
                "start_line": 1,
                "end_line": len(body.splitlines()),
                "line_count": len(body.splitlines()),
            },
        )
    if changed:
        memory["inspection_steps"] = 0
    elif result.name in READ_TOOLS:
        memory["inspection_steps"] += 1
    state = correction(checkpoint)
    if state.get("phase") == "acting" and result.name != FOCUS_TOOL:
        state["actions_remaining"] -= 1
        if result.name in READ_TOOLS:
            state["read_calls_remaining"] -= 1
        if changed:
            state["phase"] = "applied"
        elif state["actions_remaining"] <= 0:
            state["phase"] = "exhausted"
            return "exhausted"
    if result.name == FOCUS_TOOL or state.get("phase") in {"awaiting_focus", "acting"}:
        return None
    stalled = (
        memory["inspection_steps"] >= MAX_INSPECTIONS or checkpoint.get("no_progress_steps", 0) >= 6
    )
    if stalled:
        if memory["correction_used"]:
            return "exhausted"
        request_correction(
            checkpoint, "当前工作项连续观察未形成源码进展；根据已有证据选择一项可验证改动或说明阻断"
        )
        return "requested"
    return None


def working_context(checkpoint: dict[str, Any], root: Path) -> dict[str, Any]:
    """Expose actual progress and current source only, without hidden model calls."""
    memory = item_memory(checkpoint)
    state = correction(checkpoint)
    focus = state.get("next_action", {})
    targets = set(focus.get("target_paths", [])) | set(focus.get("read_paths", []))
    cache = checkpoint.get("source_evidence", {})
    selected, stale, omitted = [], [], []
    size = 0
    for path, entry in sorted(
        cache.items(),
        key=lambda pair: (
            pair[0] not in targets,
            pair[0] not in memory["changed_files"],
            pair[1].get("work_item_id") != checkpoint.get("current_work_item_id"),
            -pair[1].get("model_turn", 0),
        ),
    ):
        try:
            target = safe_path_under_root(root, path)
            if (
                target.stat().st_size > MAX_FILE_BYTES
                or sha256_bytes(target.read_bytes()) != entry["content_hash"]
            ):
                stale.append(path)
                continue
        except (AppException, OSError):
            stale.append(path)
            continue
        if size + len(entry["content"]) > MAX_CONTEXT_CHARS:
            omitted.append(path)
            continue
        selected.append(entry)
        size += len(entry["content"])
    checks = [
        {"check_id": k, "ok": v["ok"], "source_hash": v.get("data", {}).get("source_hash")}
        for k, v in checkpoint.get("check_cache", {}).items()
    ]
    return {
        "work_item_id": checkpoint.get("current_work_item_id"),
        "changed_files": memory["changed_files"],
        "recent_actions": memory["recent_actions"],
        "inspection_steps": memory["inspection_steps"],
        "source_evidence": selected,
        "stale_evidence_paths": stale,
        "omitted_evidence_paths": omitted,
        "checks_at_recorded_source": checks,
        "correction": state,
        "completion": "仅平台完成门禁可以确认工作项完成；changed_files 不证明功能已完成",
    }
