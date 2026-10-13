"""Bounded workspace index for engineering decisions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.tools import files as file_tools

MAX_CONTEXT_FILES = 160


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
