"""Read/search/patch generated-app files inside one workspace."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from app.core.exceptions import ConflictException
from app.schemas.agent_action import ToolExecutionResult
from app.tools.paths import (
    MAX_FULL_READ_BYTES,
    MAX_FULL_READ_LINES,
    MAX_LISTED_FILES,
    MAX_READ_RANGE_LINES,
    MAX_SEARCH_HITS,
    MAX_WRITE_BYTES,
    SKIP_DIRS,
    is_text_file,
    safe_path_under_root,
    sha256_bytes,
)

_REVISION_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_REVISION_MANIFEST = "manifest.json"


def _revision_root(root: Path, revision_id: str) -> Path:
    if not _REVISION_ID.fullmatch(revision_id):
        raise ConflictException("修订编号格式无效")
    # This is platform-owned metadata. Model-facing safe_path_under_root correctly
    # rejects forgeai/, so the trusted helper builds the path from a validated id.
    return root.resolve() / "forgeai" / "revisions" / revision_id


def _load_revision_manifest(revision_root: Path) -> dict[str, dict[str, object]]:
    manifest_path = revision_root / _REVISION_MANIFEST
    if not manifest_path.exists():
        return {}
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConflictException("工作区修订清单损坏") from exc
    if not isinstance(value, dict) or not all(
        isinstance(path, str) and isinstance(entry, dict) for path, entry in value.items()
    ):
        raise ConflictException("工作区修订清单格式无效")
    return value


def snapshot_workspace_source(root: Path, *, revision_id: str, path: str) -> dict[str, object]:
    """Save one path's pre-write bytes once for a recoverable engineering revision."""

    normalized = path.replace("\\", "/").strip()
    if not normalized or normalized == "forgeai" or normalized.startswith("forgeai/"):
        raise ConflictException("不能为平台内部路径创建源码修订")
    target = safe_path_under_root(root, normalized, allow_create=True)
    revision_root = _revision_root(root, revision_id)
    revision_root.mkdir(parents=True, exist_ok=True)
    manifest = _load_revision_manifest(revision_root)
    saved = manifest.get(normalized)
    if saved is not None:
        return saved

    entry: dict[str, object] = {"exists": target.exists()}
    if target.exists():
        if not target.is_file():
            raise ConflictException(f"{normalized} 不是可修订的文件")
        raw = target.read_bytes()
        backup_name = f"{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}.bin"
        (revision_root / backup_name).write_bytes(raw)
        entry.update(
            backup=backup_name,
            content_hash=sha256_bytes(raw),
            size_bytes=len(raw),
        )
    manifest[normalized] = entry
    manifest_path = revision_root / _REVISION_MANIFEST
    temporary = revision_root / f"{_REVISION_MANIFEST}.tmp"
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return entry


def restore_workspace_revision(root: Path, *, revision_id: str) -> list[str]:
    """Restore every path captured for one engineering execution revision."""

    revision_root = _revision_root(root, revision_id)
    manifest = _load_revision_manifest(revision_root)
    if not manifest:
        raise ConflictException("工作区修订不存在或没有文件")
    restored: list[str] = []
    for path, entry in manifest.items():
        target = safe_path_under_root(root, path, allow_create=True)
        if entry.get("exists") is False:
            if target.exists():
                if not target.is_file():
                    raise ConflictException(f"{path} 不是可恢复的文件")
                target.unlink()
            restored.append(path)
            continue
        backup_name = entry.get("backup")
        expected_hash = entry.get("content_hash")
        if not isinstance(backup_name, str) or not isinstance(expected_hash, str):
            raise ConflictException(f"{path} 的修订记录不完整")
        backup = safe_path_under_root(revision_root, backup_name)
        raw = backup.read_bytes()
        if sha256_bytes(raw) != expected_hash:
            raise ConflictException(f"{path} 的修订内容校验失败")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        restored.append(path)
    return restored


def list_files(
    root: Path, *, path: str = ".", tool_call_id: str = "call_list"
) -> ToolExecutionResult:
    base = root.resolve()
    start = base if path in ("", ".", "/") else safe_path_under_root(base, path, allow_create=True)
    if start.is_file():
        start = start.parent
    if not start.exists():
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="list_files",
            ok=False,
            error_code="PATH_NOT_FOUND",
            summary=f"目录不存在: {path}",
        )
    files: list[dict[str, object]] = []
    truncated = False
    for candidate in sorted(start.rglob("*")):
        try:
            is_file = candidate.is_file()
        except OSError:
            # Generated dependency/build trees can disappear while a background
            # worker replaces them. A directory listing is observational, so a
            # vanished entry must not fail the whole engineering execution.
            continue
        if not is_file:
            continue
        relative_parts = candidate.relative_to(base).parts
        if any(part in SKIP_DIRS for part in relative_parts):
            continue
        relative = candidate.relative_to(base).as_posix()
        if relative.startswith("forgeai/") or not is_text_file(candidate):
            continue
        try:
            size_bytes = candidate.stat().st_size
        except OSError:
            continue
        files.append({"path": relative, "size_bytes": size_bytes})
        if len(files) >= MAX_LISTED_FILES:
            truncated = True
            break
    return ToolExecutionResult(
        tool_call_id=tool_call_id,
        name="list_files",
        ok=True,
        summary=f"列出 {len(files)} 个文件",
        data={"files": files, "truncated": truncated},
        truncated=truncated,
    )


def read_file(
    root: Path,
    *,
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
    tool_call_id: str = "call_read",
) -> ToolExecutionResult:
    """Bounded read for agent tools. Prefer ranges for large files."""
    target = safe_path_under_root(root, path)
    if not is_text_file(target):
        raise ConflictException(f"{path} 不是可编辑的文本文件")
    raw = target.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="read_file",
            ok=False,
            error_code="NOT_TEXT",
            summary=f"{path} 不是 UTF-8 文本",
        )
    lines = text.splitlines()
    if end_line is None and (len(raw) > MAX_FULL_READ_BYTES or len(lines) > MAX_FULL_READ_LINES):
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="read_file",
            ok=False,
            error_code="FILE_TOO_LARGE",
            summary=f"{path} 过大，请先搜索符号或指定不超过 {MAX_READ_RANGE_LINES} 行的范围",
            data={
                "path": path.replace("\\", "/"),
                "size_bytes": len(raw),
                "line_count": len(lines),
                "content_hash": sha256_bytes(raw),
                "max_range_lines": MAX_READ_RANGE_LINES,
            },
        )
    if not lines:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="read_file",
            ok=True,
            summary=f"读取 {path}（空文件）",
            data={
                "path": path.replace("\\", "/"),
                "start_line": 1,
                "end_line": 0,
                "line_count": 0,
                "content": "",
                "content_hash": sha256_bytes(raw),
            },
        )
    start = max(1, start_line)
    if start > len(lines):
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="read_file",
            ok=False,
            error_code="RANGE_INVALID",
            summary=f"{path} 起始行超出文件（共 {len(lines)} 行）",
            data={
                "path": path.replace("\\", "/"),
                "line_count": len(lines),
                "content_hash": sha256_bytes(raw),
                "max_range_lines": MAX_READ_RANGE_LINES,
            },
        )
    end = min(len(lines), end_line) if end_line is not None else len(lines)
    if end < start:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="read_file",
            ok=False,
            error_code="RANGE_INVALID",
            summary="行范围无效：结束行必须不小于起始行",
            data={
                "path": path.replace("\\", "/"),
                "line_count": len(lines),
                "content_hash": sha256_bytes(raw),
                "max_range_lines": MAX_READ_RANGE_LINES,
            },
        )
    if end_line is not None and end - start + 1 > MAX_READ_RANGE_LINES:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="read_file",
            ok=False,
            error_code="RANGE_TOO_LARGE",
            summary=f"单次最多读取 {MAX_READ_RANGE_LINES} 行，请缩小范围",
            data={
                "path": path.replace("\\", "/"),
                "line_count": len(lines),
                "content_hash": sha256_bytes(raw),
                "max_range_lines": MAX_READ_RANGE_LINES,
            },
        )
    excerpt = "\n".join(lines[start - 1 : end])
    if len(excerpt.encode("utf-8")) > MAX_FULL_READ_BYTES:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="read_file",
            ok=False,
            error_code="RANGE_TOO_LARGE",
            summary="所选范围内容仍过大，请继续缩小范围",
            data={
                "path": path.replace("\\", "/"),
                "line_count": len(lines),
                "content_hash": sha256_bytes(raw),
                "max_range_lines": MAX_READ_RANGE_LINES,
            },
        )
    return ToolExecutionResult(
        tool_call_id=tool_call_id,
        name="read_file",
        ok=True,
        summary=f"读取 {path} 第 {start}-{end} 行",
        data={
            "path": path.replace("\\", "/"),
            "start_line": start,
            "end_line": end,
            "line_count": len(lines),
            "content": excerpt,
            "content_hash": sha256_bytes(raw),
        },
    )


def read_workspace_source(
    root: Path,
    *,
    path: str,
    tool_call_id: str = "call_read_full",
) -> ToolExecutionResult:
    """Full-file read for platform context assembly, capped at the write size limit."""

    target = safe_path_under_root(root, path)
    if not is_text_file(target):
        raise ConflictException(f"{path} 不是可编辑的文本文件")
    raw = target.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="read_file",
            ok=False,
            error_code="NOT_TEXT",
            summary=f"{path} 不是 UTF-8 文本",
        )
    if len(raw) > MAX_WRITE_BYTES:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="read_file",
            ok=False,
            error_code="FILE_TOO_LARGE",
            summary=f"{path} 超过可写入上限（{MAX_WRITE_BYTES} 字节），无法装配完整上下文",
            data={
                "path": path.replace("\\", "/"),
                "size_bytes": len(raw),
                "line_count": len(text.splitlines()),
                "content_hash": sha256_bytes(raw),
            },
        )
    lines = text.splitlines()
    return ToolExecutionResult(
        tool_call_id=tool_call_id,
        name="read_file",
        ok=True,
        summary=(f"读取 {path}（完整 {len(lines)} 行）" if lines else f"读取 {path}（空文件）"),
        data={
            "path": path.replace("\\", "/"),
            "start_line": 1,
            "end_line": len(lines),
            "line_count": len(lines),
            "content": text,
            "content_hash": sha256_bytes(raw),
        },
    )


def search_code(
    root: Path,
    *,
    query: str,
    path_filter: str | None = None,
    tool_call_id: str = "call_search",
) -> ToolExecutionResult:
    needle = query.strip()
    if not needle:
        raise ConflictException("搜索关键字不能为空")
    base = root.resolve()
    hits: list[dict[str, object]] = []
    truncated = False
    for candidate in sorted(base.rglob("*")):
        if not candidate.is_file() or not is_text_file(candidate):
            continue
        relative = candidate.relative_to(base).as_posix()
        if relative.startswith("forgeai/"):
            continue
        if any(part in SKIP_DIRS for part in candidate.relative_to(base).parts):
            continue
        if path_filter and path_filter not in relative:
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for index, line in enumerate(text.splitlines(), start=1):
            if needle not in line and needle.lower() not in relative.lower():
                continue
            if needle not in line:
                continue
            hits.append({"path": relative, "line": index, "text": line[:240]})
            if len(hits) >= MAX_SEARCH_HITS:
                truncated = True
                break
        if truncated:
            break
    return ToolExecutionResult(
        tool_call_id=tool_call_id,
        name="search_code",
        ok=True,
        summary=f"命中 {len(hits)} 处",
        data={"query": needle, "hits": hits, "truncated": truncated},
        truncated=truncated,
    )


def apply_patch(
    root: Path,
    *,
    path: str,
    content: str,
    expected_hash: str | None,
    tool_call_id: str = "call_patch",
) -> ToolExecutionResult:
    payload = content.encode("utf-8")
    if len(payload) > MAX_WRITE_BYTES:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="apply_patch",
            ok=False,
            error_code="FILE_TOO_LARGE",
            summary="写入内容超过上限",
        )
    target = safe_path_under_root(root, path, allow_create=True)
    if target.exists():
        current = target.read_bytes()
        current_hash = sha256_bytes(current)
        if not expected_hash:
            return ToolExecutionResult(
                tool_call_id=tool_call_id,
                name="apply_patch",
                ok=False,
                error_code="READ_REQUIRED",
                summary=f"写入已有文件 {path} 前必须先读取并提供 expected_hash",
                data={"path": path, "content_hash": current_hash},
            )
        if expected_hash != current_hash:
            return ToolExecutionResult(
                tool_call_id=tool_call_id,
                name="apply_patch",
                ok=False,
                error_code="HASH_CONFLICT",
                summary=f"{path} 已变化，请重新读取后再补丁",
                data={"path": path, "content_hash": current_hash},
            )
    elif expected_hash:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="apply_patch",
            ok=False,
            error_code="HASH_CONFLICT",
            summary=f"{path} 不存在，无法按旧 hash 写入",
        )
    if target.exists() and not is_text_file(target):
        raise ConflictException(f"{path} 不是可编辑的文本文件")
    if not is_text_file(target if target.exists() else Path(path)):
        suffix = Path(path).suffix.lower()
        if suffix not in {
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".json",
            ".md",
            ".html",
            ".css",
            ".scss",
            ".toml",
        }:
            raise ConflictException("不允许写入该文件类型")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return ToolExecutionResult(
        tool_call_id=tool_call_id,
        name="apply_patch",
        ok=True,
        summary=f"已写入 {path}",
        data={"path": path.replace("\\", "/"), "content_hash": sha256_bytes(payload)},
    )


def edit_file_by_replace(
    root: Path,
    *,
    path: str,
    old_text: str,
    new_text: str,
    expected_hash: str,
    tool_call_id: str = "call_replace",
) -> ToolExecutionResult:
    """Replace one exact block in an observed UTF-8 file."""

    target = safe_path_under_root(root, path)
    if not is_text_file(target):
        raise ConflictException(f"{path} 不是可编辑的文本文件")
    raw = target.read_bytes()
    current_hash = sha256_bytes(raw)
    if not expected_hash:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="edit_file_by_replace",
            ok=False,
            error_code="READ_REQUIRED",
            summary=f"修改已有文件 {path} 前必须先读取并提供 expected_hash",
            data={"path": path.replace("\\", "/"), "content_hash": current_hash},
        )
    if expected_hash != current_hash:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="edit_file_by_replace",
            ok=False,
            error_code="HASH_CONFLICT",
            summary=f"{path} 已变化，请重新读取后再修改",
            data={"path": path.replace("\\", "/"), "content_hash": current_hash},
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="edit_file_by_replace",
            ok=False,
            error_code="NOT_TEXT",
            summary=f"{path} 不是 UTF-8 文本",
        )
    if not old_text:
        raise ConflictException("替换原文不能为空")
    match_count = text.count(old_text)
    if match_count == 0:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="edit_file_by_replace",
            ok=False,
            error_code="NO_MATCH",
            summary="替换原文未精确匹配，请重新读取目标范围",
            data={"path": path.replace("\\", "/"), "content_hash": current_hash},
        )
    if match_count > 1:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="edit_file_by_replace",
            ok=False,
            error_code="AMBIGUOUS_MATCH",
            summary=f"替换原文命中 {match_count} 处，请扩大上下文使其唯一",
            data={"path": path.replace("\\", "/"), "content_hash": current_hash},
        )
    payload = text.replace(old_text, new_text, 1).encode("utf-8")
    if len(payload) > MAX_WRITE_BYTES:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="edit_file_by_replace",
            ok=False,
            error_code="FILE_TOO_LARGE",
            summary="修改后的文件超过写入上限",
        )
    target.write_bytes(payload)
    return ToolExecutionResult(
        tool_call_id=tool_call_id,
        name="edit_file_by_replace",
        ok=True,
        summary=f"已局部修改 {path}",
        data={"path": path.replace("\\", "/"), "content_hash": sha256_bytes(payload)},
    )
