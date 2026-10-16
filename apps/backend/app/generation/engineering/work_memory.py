"""Source-bound working evidence; observations do not change tool permissions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.core.exceptions import AppException
from app.schemas.agent_action import ToolExecutionResult
from app.tools.paths import MAX_FILE_BYTES, safe_path_under_root, sha256_bytes

READ_TOOLS = frozenset({"read_file", "list_files", "search_code"})
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
        },
    )


def _cache_evidence(checkpoint: dict[str, Any], data: dict[str, Any]) -> None:
    path = data.get("path")
    if not isinstance(path, str) or not isinstance(data.get("content"), str):
        return
    cache = checkpoint.setdefault("source_evidence", {})
    old = cache.get(path)
    same_version = old and old.get("content_hash") == data.get("content_hash")
    fragments = list(old["fragments"]) if same_version else []
    # A file version can have several complementary ranges. Only identical
    # observations are duplicates; reading the tail must not erase the head.
    if any(
        all(entry.get(k) == data.get(k) for k in ("start_line", "end_line")) for entry in fragments
    ):
        return
    body = data["content"]
    fragments.append(
        {
            **{
                k: data.get(k)
                for k in ("path", "content_hash", "start_line", "end_line", "line_count")
            },
            "content": body[:MAX_EVIDENCE_CHARS],
            "content_truncated": bool(data.get("content_truncated"))
            or len(body) > MAX_EVIDENCE_CHARS,
            "work_item_id": data.get("work_item_id", checkpoint.get("current_work_item_id")),
            "model_turn": data.get("model_turn", checkpoint.get("model_turns", 0)),
        }
    )
    # Bound each file's durable excerpts as well as the aggregate model context.
    while sum(len(entry["content"]) for entry in fragments) > MAX_CONTEXT_CHARS:
        fragments.pop(0)
    cache[path] = {
        "content_hash": data.get("content_hash"),
        "work_item_id": fragments[-1]["work_item_id"],
        "model_turn": fragments[-1]["model_turn"],
        "fragments": fragments,
    }
    # Prefer the current work item's changed files.
    memory = item_memory(checkpoint)
    pinned = set(memory["changed_files"]) | {path}
    while len(cache) > MAX_EVIDENCE_FILES:
        victim = min(cache, key=lambda p: (p in pinned, cache[p].get("model_turn", 0)))
        del cache[victim]


def seed_source_evidence(checkpoint: dict[str, Any]) -> None:
    """Restore ranges from legacy cache/history without reviving older source versions."""
    cache = checkpoint.setdefault("source_evidence", {})
    for path, entry in list(cache.items()):
        if "fragments" not in entry:
            del cache[path]
            _cache_evidence(checkpoint, entry)
    reads = [
        row.get("data", {})
        for row in checkpoint.get("observations", [])
        if row.get("ok") and row.get("name") == "read_file"
    ]
    versions = {data.get("path"): data.get("content_hash") for data in reads}
    versions.update({path: entry.get("content_hash") for path, entry in cache.items()})
    versions.update(checkpoint.get("observed_hashes", {}))
    for data in reads:
        if data.get("content_hash") == versions.get(data.get("path")):
            _cache_evidence(checkpoint, data)


def record_result(
    checkpoint: dict[str, Any],
    result: ToolExecutionResult,
    *,
    updates: dict[str, bytes] | None = None,
) -> None:
    """Record committed source and observations without inferring completion."""
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


def working_context(checkpoint: dict[str, Any], root: Path) -> dict[str, Any]:
    """Expose actual progress and current source only, without hidden model calls."""
    memory = item_memory(checkpoint)
    cache = checkpoint.get("source_evidence", {})
    selected, stale, omitted, omitted_ranges = [], [], [], []
    source_hashes = {}
    size = 0
    for path, entry in sorted(
        cache.items(),
        key=lambda pair: (
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
        source_hashes[path] = entry["content_hash"]
        for fragment in reversed(entry["fragments"]):
            if size + len(fragment["content"]) > MAX_CONTEXT_CHARS:
                if path not in omitted:
                    omitted.append(path)
                omitted_ranges.append(
                    {k: fragment.get(k) for k in ("path", "start_line", "end_line")}
                )
                continue
            selected.append(fragment)
            size += len(fragment["content"])
    checks = [
        {"check_id": k, "ok": v["ok"], "source_hash": v.get("data", {}).get("source_hash")}
        for k, v in checkpoint.get("check_cache", {}).items()
    ]
    return {
        "work_item_id": checkpoint.get("current_work_item_id"),
        "changed_files": memory["changed_files"],
        "recent_actions": [
            action
            for action in memory["recent_actions"]
            if action.get("tool") not in {"model_response", "update_work_item_focus"}
        ],
        "inspection_steps": memory["inspection_steps"],
        "source_evidence": selected,
        "source_hashes": source_hashes,
        "stale_evidence_paths": stale,
        "omitted_evidence_paths": omitted,
        "omitted_evidence_ranges": omitted_ranges,
        "checks_at_recorded_source": checks,
        "completion": "仅平台完成门禁可以确认工作项完成；changed_files 不证明功能已完成",
    }


# These are recovery heuristics, not claims about model capability. Tool access
# stays unchanged; a model gets several decisions after guidance before stopping.
GUIDANCE_AFTER_STEPS = 12
MAX_DUPLICATE_STEPS = 24
MAX_INSPECTION_STEPS = 48
GUIDANCE_GRACE_TURNS = 3


def record_progress(
    checkpoint: dict[str, Any],
    result: ToolExecutionResult,
    *,
    changed: bool = False,
    plan_changed: bool = False,
) -> None:
    """Count actual new evidence separately from source/verification progress."""
    information = False
    verification = False
    data = result.data
    if result.ok and result.name == "read_file" and isinstance(data.get("content"), str):
        path, digest = data.get("path"), data.get("content_hash")
        if isinstance(path, str) and isinstance(digest, str):
            reads = checkpoint.setdefault("read_coverage", {})
            previous = reads.get(path, {})
            ranges = previous.get("ranges", []) if previous.get("content_hash") == digest else []
            start, end = data.get("start_line", 1), data.get("end_line", 0)
            # Model history and source memory expose at most this prefix. Do not
            # count an omitted tail (or a cut-off line) as already inspected.
            visible = data["content"][:MAX_EVIDENCE_CHARS]
            lines = len(visible.splitlines())
            if (len(visible) < len(data["content"]) or data.get("content_truncated")) and (
                not visible.endswith(("\n", "\r"))
            ):
                lines = max(0, lines - 1)
            end = min(end, start + lines - 1)
            if start <= end:
                information = not any(a <= start and b >= end for a, b in ranges)
                merged: list[list[int]] = []
                for a, b in sorted([*ranges, [start, end]]):
                    if merged and a <= merged[-1][1] + 1:
                        merged[-1][1] = max(merged[-1][1], b)
                    else:
                        merged.append([a, b])
                reads[path] = {"content_hash": digest, "ranges": merged}
    elif result.ok and result.name in {"list_files", "search_code"}:
        # Queries are not evidence: two searches returning the same matches do
        # not become progress just because their arguments differ.
        evidence = {k: v for k, v in data.items() if k not in {"query", "path_filter"}}
        fingerprint = hashlib.sha256(
            json.dumps([result.name, evidence], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        seen = checkpoint.setdefault("context_fingerprints", [])
        information = fingerprint not in seen
        if information:
            seen.append(fingerprint)
    elif result.name in {"run_check", "complete_work_item"} and data.get("source_hash"):
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    data.get("check_id"),
                    data["source_hash"],
                    result.ok,
                    result.error_code,
                    data.get("output"),
                ],
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        seen = checkpoint.setdefault("verification_fingerprints", [])
        verification = fingerprint not in seen
        if verification:
            seen.append(fingerprint)
    delivery_progress = changed or verification
    checkpoint["no_progress_steps"] = (
        0
        if information or delivery_progress or plan_changed
        else int(checkpoint.get("no_progress_steps", 0)) + 1
    )
    checkpoint["steps_without_delivery"] = (
        0 if delivery_progress else int(checkpoint.get("steps_without_delivery", 0)) + 1
    )
    if delivery_progress:
        checkpoint.pop("progress_guidance_turn", None)


def progress_guidance(checkpoint: dict[str, Any]) -> dict[str, Any] | None:
    """One replaceable current-state note; no accumulating correction transcript."""
    if (
        checkpoint.get("needs_delivery_plan")
        or int(checkpoint.get("steps_without_delivery", 0)) < GUIDANCE_AFTER_STEPS
    ):
        return None
    return {
        "work_item_id": checkpoint.get("current_work_item_id"),
        "steps_without_source_or_new_check": checkpoint["steps_without_delivery"],
        "steps_without_new_evidence": checkpoint.get("no_progress_steps", 0),
    }


def implementation_stalled(checkpoint: dict[str, Any]) -> bool:
    issued = checkpoint.get("progress_guidance_turn")
    return (
        not checkpoint.get("needs_delivery_plan")
        and isinstance(issued, int)
        and int(checkpoint.get("model_turns", 0)) - issued >= GUIDANCE_GRACE_TURNS
        and (
            int(checkpoint.get("no_progress_steps", 0)) >= MAX_DUPLICATE_STEPS
            or int(checkpoint.get("steps_without_delivery", 0)) >= MAX_INSPECTION_STEPS
        )
    )
