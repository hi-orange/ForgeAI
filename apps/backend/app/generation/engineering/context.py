"""Bounded workspace index for engineering decisions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.exceptions import AppException
from app.tools import files as file_tools
from app.tools.paths import MAX_FULL_READ_BYTES, safe_path_under_root, sha256_bytes

MAX_CONTEXT_FILES = 160
MAX_INITIAL_CONTEXT_CHARS = 24000
MAX_INITIAL_FILE_CHARS = 6000
# Platform-owned entry points, read from the actual isolated workspace, never
# from today's template checkout or from a model-controlled path manifest.
INITIAL_CONTEXT_PATHS = (
    "README.md",
    "template.json",
    "forgeai.smoke.json",
    "frontend/package.json",
    "frontend/src/App.tsx",
    "frontend/src/router.tsx",
    "frontend/src/main.tsx",
    "frontend/src/lib/api.ts",
    "frontend/src/index.css",
    "backend/pyproject.toml",
    "backend/app/main.py",
    "backend/app/api/router.py",
    "backend/app/api/health.py",
    "backend/app/core/settings.py",
    "backend/app/db/database.py",
    "backend/app/models/__init__.py",
    "backend/app/schemas/response.py",
    "backend/alembic/env.py",
)


def build_initial_source_context(root: Path) -> dict[str, Any]:
    """Read small entry points together without model turns or fabricated tool calls."""
    sources, omitted = [], []
    remaining = MAX_INITIAL_CONTEXT_CHARS
    for path in INITIAL_CONTEXT_PATHS:
        try:
            target = safe_path_under_root(root, path)
            if target.stat().st_size > MAX_FULL_READ_BYTES:
                omitted.append({"path": path, "reason": "file_too_large"})
                continue
            result = file_tools.read_file(root, path=path)
        except (AppException, OSError):
            omitted.append({"path": path, "reason": "unavailable"})
            continue
        if not result.ok:
            omitted.append({"path": path, "reason": result.error_code or "unavailable"})
            continue
        data = dict(result.data)
        body = data["content"]
        limit = min(remaining, MAX_INITIAL_FILE_CHARS)
        if body and limit == 0:
            omitted.append({"path": path, "reason": "context_budget"})
            continue
        if len(body) > limit:
            # Only claim complete lines as observed; the omitted tail stays explicit.
            body = body[:limit].rsplit("\n", 1)[0] if "\n" in body[:limit] else ""
            if not body:
                omitted.append(
                    {
                        "path": path,
                        "reason": "context_budget"
                        if remaining < MAX_INITIAL_FILE_CHARS
                        else "line_too_large",
                    }
                )
                continue
        data.update(
            content=body,
            end_line=len(body.splitlines()),
            content_truncated=len(body) < len(result.data["content"]),
        )
        sources.append(data)
        remaining -= len(body)
    return {"files": sources, "omitted_files": omitted, "origin": "current_workspace"}


def initial_source_context(
    checkpoint: dict[str, Any], root: Path, *, source_evidence: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Project the saved startup pack only while each file still has the same bytes."""
    saved = checkpoint.get("initial_source_context") or {}
    sources, stale = [], []
    for data in saved.get("files", []):
        path = data["path"]
        try:
            target = safe_path_under_root(root, path)
            if (
                target.stat().st_size > MAX_FULL_READ_BYTES
                or sha256_bytes(target.read_bytes()) != data["content_hash"]
            ):
                stale.append(path)
                continue
        except (AppException, OSError):
            stale.append(path)
            continue
        # Only a range actually projected into this model turn can replace the
        # startup excerpt. A cached tail or a budget-omitted read cannot erase its head.
        if not any(
            entry.get("path") == path
            and entry.get("content_hash") == data["content_hash"]
            and not entry.get("content_truncated")
            and entry.get("start_line", 1) <= data["start_line"]
            and entry.get("end_line", 0) >= data["end_line"]
            for entry in source_evidence or []
        ):
            sources.append(data)
    return {
        "origin": "current_workspace",
        "files": sources,
        "omitted_files": saved.get("omitted_files", []),
        "stale_paths": stale,
    }


def build_workspace_file_index(root: Path) -> dict[str, Any]:
    listing = file_tools.list_files(root, tool_call_id="context_file_index")
    raw_files = listing.data.get("files") if listing.ok else []
    files = raw_files if isinstance(raw_files, list) else []
    paths = [
        str(item.get("path"))
        for item in files[:MAX_CONTEXT_FILES]
        if isinstance(item, dict) and item.get("path")
    ]
    return {
        "paths": paths,
        "visible_file_count": len(files),
        "truncated": bool(listing.truncated or len(files) > MAX_CONTEXT_FILES),
    }


def update_file_index(checkpoint: dict[str, Any], path: str) -> None:
    index = checkpoint.get("workspace_file_index")
    if not isinstance(index, dict):
        return
    paths = list(index.get("paths") or [])
    if path not in paths and len(paths) < MAX_CONTEXT_FILES:
        paths.append(path)
        index["paths"] = sorted(paths)
        index["visible_file_count"] = max(int(index.get("visible_file_count") or 0), len(paths))
