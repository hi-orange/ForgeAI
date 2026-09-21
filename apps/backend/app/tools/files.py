"""Read/search/patch generated-app files inside one workspace."""

from __future__ import annotations

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
        if not candidate.is_file():
            continue
        relative_parts = candidate.relative_to(base).parts
        if any(part in SKIP_DIRS for part in relative_parts):
            continue
        relative = candidate.relative_to(base).as_posix()
        if relative.startswith("forgeai/") or not is_text_file(candidate):
            continue
        files.append({"path": relative, "size_bytes": candidate.stat().st_size})
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
    target = safe_path_under_root(root, path)
    if not is_text_file(target):
        raise ConflictException("该文件不是可编辑的文本文件")
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
    start = max(1, start_line)
    end = min(len(lines), end_line) if end_line is not None else len(lines)
    if end < start:
        raise ConflictException("行范围无效")
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
        raise ConflictException("该文件不是可编辑的文本文件")
    if not is_text_file(target if target.exists() else Path(path)):
        suffix = Path(path).suffix.lower()
        if suffix not in {".py", ".ts", ".vue", ".json", ".md", ".html", ".css", ".scss", ".toml"}:
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
        raise ConflictException("该文件不是可编辑的文本文件")
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
