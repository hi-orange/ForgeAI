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
MAX_EVIDENCE_CHARS = 12000
# Native tool results have a 20k serialized-JSON ceiling. Reserve room for
# result metadata, including escaped source text, before declaring lines seen.
MAX_EVIDENCE_JSON_CHARS = 14000
MAX_CONTEXT_CHARS = 24000
MAX_REDUNDANT_READ_STEPS = 4
GUIDANCE_AFTER_REDUNDANT_READS = 2
# Count decisions spent only inspecting, not files returned by a batch. A wide
# work item can inspect many files at once; a long series of read-only decisions
# must instead produce an implementation, a check, or a narrower valid plan.
MAX_INSPECTION_MODEL_TURNS = 16
# A cache-budget miss can re-expose a previously read file without setting
# already_visible. Move a long no-information inspection into the same action
# window before the generic no-progress stop fires.
STALLED_INSPECTION_MODEL_TURNS = 8
GUIDANCE_AFTER_STEPS = 12


def visible_read_fragment(data: dict[str, Any]) -> dict[str, Any]:
    """Describe only complete lines that fit in one model-visible excerpt."""

    body = data.get("content")
    if not isinstance(body, str):
        return dict(data)
    legacy_truncation = bool(data.get("content_truncated")) and "requested_end_line" not in data
    limit = min(len(body), MAX_EVIDENCE_CHARS)
    if len(json.dumps(body[:limit], ensure_ascii=False)) > MAX_EVIDENCE_JSON_CHARS:
        low, high = 0, limit
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(body[:middle], ensure_ascii=False)) <= MAX_EVIDENCE_JSON_CHARS:
                low = middle
            else:
                high = middle - 1
        limit = low
    if limit == len(body) and not legacy_truncation:
        return dict(data)
    prefix = body[:limit]
    last_break = prefix.rfind("\n")
    truncated = limit < len(body) or legacy_truncation
    visible = prefix[: last_break + 1] if truncated and last_break >= 0 else prefix
    start = int(data.get("start_line") or 1)
    visible_end = (
        start + len(visible.splitlines()) - 1 if last_break >= 0 or not truncated else start - 1
    )
    partial_line = last_break < 0 and truncated
    return {
        **data,
        "content": visible,
        "end_line": visible_end,
        "requested_end_line": data.get("requested_end_line", data.get("end_line")),
        "content_truncated": True,
        **({"partial_line": True, "line_too_long": True} if partial_line else {}),
        **({"next_start_line": visible_end + 1} if not partial_line else {}),
    }


def item_memory(checkpoint: dict[str, Any]) -> dict[str, Any]:
    key = str(checkpoint.get("current_work_item_id") or "")
    return checkpoint.setdefault("work_memory", {}).setdefault(
        key,
        {
            "changed_files": {},
            "recent_actions": [],
            "inspection_steps": 0,
            "inspection_model_turns": 0,
        },
    )


def inspection_action_required(checkpoint: dict[str, Any]) -> bool:
    """Ask the engineer to act on its evidence after prolonged read-only work."""

    memory = item_memory(checkpoint)
    return not checkpoint.get("needs_delivery_plan") and (
        int(memory.get("inspection_model_turns", 0)) >= MAX_INSPECTION_MODEL_TURNS
        or int(memory.get("redundant_read_steps", 0)) >= MAX_REDUNDANT_READ_STEPS
        or (
            int(memory.get("inspection_model_turns", 0)) >= STALLED_INSPECTION_MODEL_TURNS
            and int(checkpoint.get("no_progress_steps", 0)) >= GUIDANCE_AFTER_STEPS
        )
    )


def _line_range(fragment: dict[str, Any]) -> tuple[int, int] | None:
    start, end = fragment.get("start_line"), fragment.get("end_line")
    if isinstance(start, int) and isinstance(end, int) and start <= end:
        return start, end
    return None


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _explicit_ranges(fragment: dict[str, Any]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    if fragment.get("explicit_read") is True and (span := _line_range(fragment)):
        ranges.append(span)
    for pair in fragment.get("explicit_read_ranges", []):
        if (
            isinstance(pair, (list, tuple))
            and len(pair) == 2
            and isinstance(pair[0], int)
            and isinstance(pair[1], int)
            and pair[0] <= pair[1]
        ):
            ranges.append((pair[0], pair[1]))
    return ranges


def _merge_provenance(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Keep exact read coverage when source text is absorbed by a wider excerpt."""
    ranges = _merge_ranges([*_explicit_ranges(target), *_explicit_ranges(source)])
    span = _line_range(target)
    if span and any(start <= span[0] and end >= span[1] for start, end in ranges):
        target["explicit_read"] = True
        target.pop("explicit_read_ranges", None)
    elif ranges:
        target["explicit_read_ranges"] = ranges


def _coalesce_fragments(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Store one copy of contained source without claiming unobserved lines were read."""
    compact: list[dict[str, Any]] = []
    for original in fragments:
        fragment = visible_read_fragment(original)
        span = _line_range(fragment)
        if not span:
            duplicate = next(
                (
                    row
                    for row in compact
                    if row.get("start_line") == fragment.get("start_line")
                    and row.get("end_line") == fragment.get("end_line")
                    and row.get("content") == fragment.get("content")
                ),
                None,
            )
            if duplicate is not None:
                if fragment.get("explicit_read") is True:
                    duplicate["explicit_read"] = True
                continue
        covering = next(
            (
                row
                for row in compact
                if span
                and (outer := _line_range(row))
                and outer[0] <= span[0]
                and outer[1] >= span[1]
            ),
            None,
        )
        if covering is not None:
            _merge_provenance(covering, fragment)
            continue
        if span:
            contained = [
                row
                for row in compact
                if (inner := _line_range(row)) and span[0] <= inner[0] and span[1] >= inner[1]
            ]
            for row in contained:
                _merge_provenance(fragment, row)
                compact.remove(row)
        compact.append(fragment)
    return compact


def _cache_evidence(
    checkpoint: dict[str, Any], data: dict[str, Any], *, explicit_read: bool = False
) -> None:
    path = data.get("path")
    if not isinstance(path, str) or not isinstance(data.get("content"), str):
        return
    cache = checkpoint.setdefault("source_evidence", {})
    old = cache.get(path)
    same_version = old and old.get("content_hash") == data.get("content_hash")
    fragments = _coalesce_fragments(old["fragments"]) if same_version else []
    visible = visible_read_fragment(data)
    body = visible["content"]
    fragments.append(
        {
            **{
                k: visible.get(k)
                for k in ("path", "content_hash", "start_line", "end_line", "line_count")
            },
            "content": body,
            "content_truncated": bool(visible.get("content_truncated")),
            "explicit_read": explicit_read,
            **{
                k: visible[k]
                for k in ("requested_end_line", "next_start_line", "partial_line", "line_too_long")
                if k in visible
            },
            "work_item_id": data.get("work_item_id", checkpoint.get("current_work_item_id")),
            "model_turn": data.get("model_turn", checkpoint.get("model_turns", 0)),
        }
    )
    # A write can seed the whole current file, followed by many explicit reads
    # of smaller ranges. Preserve their provenance without repeating the text.
    fragments = _coalesce_fragments(fragments)
    # Bound each file's durable excerpts as well as the aggregate model context.
    while sum(len(entry["content"]) for entry in fragments) > MAX_CONTEXT_CHARS:
        fragments.pop(0)
    cache[path] = {
        "content_hash": data.get("content_hash"),
        "work_item_id": data.get("work_item_id", checkpoint.get("current_work_item_id")),
        "model_turn": data.get("model_turn", checkpoint.get("model_turns", 0)),
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
        row
        for row in checkpoint.get("observations", [])
        if row.get("ok") and row.get("name") == "read_file"
    ]
    versions = {
        row.get("data", {}).get("path"): row.get("data", {}).get("content_hash") for row in reads
    }
    versions.update({path: entry.get("content_hash") for path, entry in cache.items()})
    versions.update(checkpoint.get("observed_hashes", {}))
    for row in reads:
        data = row.get("data", {})
        if data.get("content_hash") == versions.get(data.get("path")):
            # Historical observation.data did not carry its work-item identity.
            # Replaying it must not promote every old file to the current item.
            previous = cache.get(data.get("path"), {})
            observed_turn = row.get("model_turn")
            _cache_evidence(
                checkpoint,
                {
                    **data,
                    "work_item_id": previous.get("work_item_id"),
                    "model_turn": max(
                        int(previous.get("model_turn") or 0),
                        observed_turn if isinstance(observed_turn, int) else 0,
                    ),
                },
                explicit_read=True,
            )


def record_result(
    checkpoint: dict[str, Any],
    result: ToolExecutionResult,
    *,
    updates: dict[str, bytes] | None = None,
) -> None:
    """Record committed source and observations without inferring completion."""
    if result.ok and result.name == "read_file":
        _cache_evidence(checkpoint, result.data, explicit_read=True)
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


def suppress_redundant_read(
    checkpoint: dict[str, Any], root: Path, result: ToolExecutionResult
) -> ToolExecutionResult:
    """Avoid returning the same visible source version and range to the model."""

    if not result.ok or result.name != "read_file":
        return result
    data = result.data
    path, digest = data.get("path"), data.get("content_hash")
    start, end = data.get("start_line"), data.get("end_line")
    if not isinstance(path, str) or not isinstance(digest, str):
        return result
    if not isinstance(start, int) or not isinstance(end, int):
        return result
    fragments = [
        row
        for row in working_context(checkpoint, root)["source_evidence"]
        if row.get("path") == path and row.get("content_hash") == digest
    ]
    if not fragments:
        return result
    projected = visible_read_fragment(data)
    projected_end = int(projected.get("end_line") or 0)
    # A wider write-seeded excerpt can contain narrower explicit reads. Only
    # suppress ranges actually read by the model, and only while those bytes
    # are still projected in this turn's hash-checked source context.
    coverage = checkpoint.get("read_coverage", {}).get(path, {})
    legacy_ranges = coverage.get("ranges", []) if coverage.get("content_hash") == digest else []
    explicit_ranges: list[tuple[int, int]] = []
    for row in fragments:
        if not (span := _line_range(row)):
            continue
        for start_line, end_line in [*_explicit_ranges(row), *legacy_ranges]:
            start_line, end_line = max(start_line, span[0]), min(end_line, span[1])
            if start_line <= end_line:
                explicit_ranges.append((start_line, end_line))
    covered = start
    for row_start, row_end in sorted(explicit_ranges):
        if row_start <= covered:
            covered = max(covered, row_end + 1)
    if projected.get("partial_line"):
        if not any(
            row.get("start_line") == start
            and row.get("partial_line")
            and row.get("content") == projected.get("content")
            and row.get("explicit_read") is True
            for row in fragments
        ):
            return result
    elif covered <= projected_end:
        return result
    next_line = covered if covered <= end and not projected.get("partial_line") else None
    if projected.get("line_too_long"):
        guidance = "该行超过上下文长度，按行重读无法显示剩余内容；请搜索相关片段或局部替换。"
    elif isinstance(next_line, int) and next_line <= end:
        guidance = f"尚未显示的内容请从第 {next_line} 行定向读取。"
    else:
        guidance = "请基于现有源码修改、运行检查或完成当前工作项。"
    summary = f"{path} 当前版本的本次可见片段已在工作记忆中；重复读取不会得到新证据。{guidance}"
    return result.model_copy(
        update={
            "summary": summary,
            "data": {
                "path": path,
                "content_hash": digest,
                "start_line": start,
                "end_line": end,
                "line_count": data.get("line_count"),
                "already_visible": True,
                **({"next_start_line": next_line} if isinstance(next_line, int) else {}),
            },
        }
    )


def working_context(checkpoint: dict[str, Any], root: Path) -> dict[str, Any]:
    """Expose actual progress and current source only, without hidden model calls."""
    memory = item_memory(checkpoint)
    cache = checkpoint.get("source_evidence", {})
    # Older checkpoints can carry a write-seeded whole file plus many later
    # contained reads. Compact them before projection and future persistence.
    for entry in cache.values():
        if isinstance(entry.get("fragments"), list):
            entry["fragments"] = _coalesce_fragments(entry["fragments"])
    selected, stale, omitted, omitted_ranges = [], [], [], []
    source_hashes = {}
    size = 0

    def projection_priority(pair: tuple[str, dict[str, Any]]) -> tuple[int, int, int, str]:
        path, entry = pair
        smoke = path == "forgeai.smoke.json"
        if path in memory["changed_files"] and not smoke:
            tier = 0
        elif entry.get("work_item_id") == checkpoint.get("current_work_item_id") and not smoke:
            tier = 1
        elif smoke:
            tier = 2
        else:
            tier = 3
        # Keep current edits ahead of smaller read-only dependencies. A large
        # smoke manifest can be read on demand instead of crowding out source.
        return (
            tier,
            sum(len(fragment.get("content", "")) for fragment in entry.get("fragments", [])),
            -int(entry.get("model_turn") or 0),
            path,
        )

    for path, entry in sorted(
        cache.items(),
        key=projection_priority,
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
        for cached in reversed(entry["fragments"]):
            fragment = visible_read_fragment(cached)
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
        "inspection_model_turns": int(memory.get("inspection_model_turns", 0)),
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
    repeated_read = False
    data = result.data
    # Source edits and new failure output are evidence, but do not demonstrate
    # that an outstanding check blocker has been repaired. Keep this clock across
    # edits, rereads and recovery; only its own passing check clears it.
    watch = checkpoint.setdefault("verification_watch", {}).setdefault(
        str(checkpoint.get("current_work_item_id") or ""), {}
    )
    for check_id in watch:
        watch[check_id] += 1
    if result.name in {"run_check", "complete_work_item"} and data.get("source_hash"):
        check_id = str(data.get("check_id") or "")
        if check_id:
            if result.ok:
                watch.pop(check_id, None)
                for covered in data.get("covered_checks", []):
                    watch.pop(covered, None)
            else:
                watch.setdefault(check_id, 0)
    if result.ok and result.name == "read_file" and isinstance(data.get("content"), str):
        path, digest = data.get("path"), data.get("content_hash")
        if isinstance(path, str) and isinstance(digest, str):
            reads = checkpoint.setdefault("read_coverage", {})
            previous = reads.get(path, {})
            ranges = previous.get("ranges", []) if previous.get("content_hash") == digest else []
            start, end = data.get("start_line", 1), data.get("end_line", 0)
            # Model history and source memory expose at most this prefix. Do not
            # count an omitted tail (or a cut-off line) as already inspected.
            end = min(end, int(visible_read_fragment(data).get("end_line") or 0))
            if start <= end:
                information = not any(a <= start and b >= end for a, b in ranges)
                # The bounded source cache may have evicted this file, so the
                # tool must return its bytes again for a safe edit. The same
                # source version and visible range still adds no new evidence.
                repeated_read = not information
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
    memory = item_memory(checkpoint)
    if delivery_progress or plan_changed:
        memory["inspection_model_turns"] = 0
        memory.pop("last_inspection_model_turn", None)
    elif result.name in READ_TOOLS:
        model_turn = int(checkpoint.get("model_turns") or 0)
        if memory.get("last_inspection_model_turn") != model_turn:
            memory["inspection_model_turns"] = int(memory.get("inspection_model_turns", 0)) + 1
            memory["last_inspection_model_turn"] = model_turn
    memory["redundant_read_steps"] = (
        0
        if information or delivery_progress or plan_changed
        else int(memory.get("redundant_read_steps", 0))
        + int(result.name == "read_file" and (data.get("already_visible") is True or repeated_read))
    )
    checkpoint["no_progress_steps"] = (
        0
        if information or delivery_progress or plan_changed
        else int(checkpoint.get("no_progress_steps", 0)) + 1
    )
    checkpoint["steps_without_delivery"] = (
        0 if delivery_progress else int(checkpoint.get("steps_without_delivery", 0)) + 1
    )
    if delivery_progress and not watch:
        checkpoint.pop("progress_guidance_turn", None)


def progress_guidance(checkpoint: dict[str, Any]) -> dict[str, Any] | None:
    """One replaceable current-state note; no accumulating correction transcript."""
    watch = checkpoint.get("verification_watch", {}).get(
        str(checkpoint.get("current_work_item_id") or ""), {}
    )
    redundant_reads = int(item_memory(checkpoint).get("redundant_read_steps", 0))
    if checkpoint.get("needs_delivery_plan") or (
        max(int(checkpoint.get("steps_without_delivery", 0)), max(watch.values(), default=0))
        < GUIDANCE_AFTER_STEPS
        and redundant_reads < GUIDANCE_AFTER_REDUNDANT_READS
        and not inspection_action_required(checkpoint)
    ):
        return None
    return {
        "work_item_id": checkpoint.get("current_work_item_id"),
        "steps_without_source_or_new_check": checkpoint["steps_without_delivery"],
        "steps_without_new_evidence": checkpoint.get("no_progress_steps", 0),
        "repeated_source_reads": redundant_reads,
        "inspection_model_turns": int(item_memory(checkpoint).get("inspection_model_turns", 0)),
        "inspection_action_required": inspection_action_required(checkpoint),
        "unresolved_check_steps": dict(watch),
    }


def implementation_stalled(checkpoint: dict[str, Any]) -> bool:
    issued = checkpoint.get("progress_guidance_turn")
    watch = checkpoint.get("verification_watch", {}).get(
        str(checkpoint.get("current_work_item_id") or ""), {}
    )
    # Repeated reads first close the read tools for a decision. Blocking at the
    # same threshold would prevent the model from using that action window.
    return (
        not checkpoint.get("needs_delivery_plan")
        and isinstance(issued, int)
        and int(checkpoint.get("model_turns", 0)) - issued >= GUIDANCE_GRACE_TURNS
        and (
            int(checkpoint.get("no_progress_steps", 0)) >= MAX_DUPLICATE_STEPS
            or max(watch.values(), default=0) >= MAX_INSPECTION_STEPS
        )
    )
