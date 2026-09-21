"""Path-safe workspace access for engineering tools."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.core.exceptions import ConflictException, NotFoundException

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".turbo",
    "dist",
    "data",
}
TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".vue",
    ".json",
    ".md",
    ".toml",
    ".ini",
    ".html",
    ".css",
    ".scss",
    ".txt",
    ".mako",
    ".example",
}
MAX_FILE_BYTES = 256 * 1024
MAX_FULL_READ_BYTES = 50 * 1024
MAX_FULL_READ_LINES = 1000
MAX_READ_RANGE_LINES = 400
MAX_LISTED_FILES = 400
MAX_SEARCH_HITS = 40
MAX_WRITE_BYTES = 256 * 1024


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def safe_path_under_root(root: Path, relative_path: str, *, allow_create: bool = False) -> Path:
    cleaned = relative_path.replace("\\", "/").strip()
    if not cleaned or cleaned.startswith("/") or ".." in cleaned.split("/"):
        raise ConflictException("非法文件路径")
    parts = cleaned.split("/")
    if any(part in SKIP_DIRS for part in parts):
        raise ConflictException("不允许访问该路径")
    if cleaned.startswith("forgeai/"):
        raise ConflictException("不允许修改平台标记目录")
    root_resolved = root.resolve()
    candidate = (root / cleaned).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ConflictException("非法文件路径") from exc
    if not allow_create and not candidate.is_file():
        raise NotFoundException("文件不存在")
    return candidate


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in {
        "README",
        "LICENSE",
        ".env.example",
    }
