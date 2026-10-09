from __future__ import annotations

import base64
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException, NotFoundException
from app.core.settings import settings
from app.generation.workspace import default_workspace_path, workspace_is_ready
from app.models.user import User
from app.services import project as project_service
from app.services.requirements import get_requirements_status

_SKIP_DIRS = {
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
_TEXT_SUFFIXES = {
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
_IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
}
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}
_MAX_TEXT_BYTES = 512 * 1024
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_LISTED_FILES = 400

WorkspaceFileKind = Literal["text", "image"]


class WorkspaceFileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=0)
    kind: WorkspaceFileKind = "text"


class WorkspaceListing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    run_id: str | None = None
    root: str | None = None
    files: list[WorkspaceFileEntry] = Field(default_factory=list)


class WorkspaceFileContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    kind: WorkspaceFileKind = "text"
    content: str
    media_type: str | None = None
    truncated: bool = False
    size_bytes: int = Field(ge=0)


def _file_kind(path: Path) -> WorkspaceFileKind | None:
    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES or path.name in {"README", "LICENSE", ".env.example"}:
        return "text"
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    return None


def list_project_workspace(db: Session, user: User, project_id: int) -> WorkspaceListing:
    project_service.get_user_project(db, user, project_id)
    status = get_requirements_status(db, user, project_id)
    if (
        status.run_id is None
        or not status.workspace_ready
        or not workspace_is_ready(project_id, status.run_id)
    ):
        return WorkspaceListing(ready=False, run_id=status.run_id)
    root = default_workspace_path(settings.runtime_data_root, project_id, status.run_id)
    files: list[WorkspaceFileEntry] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(root).parts
        if any(part in _SKIP_DIRS for part in relative_parts):
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith("forgeai/"):
            continue
        kind = _file_kind(path)
        if kind is None:
            continue
        files.append(WorkspaceFileEntry(path=relative, size_bytes=path.stat().st_size, kind=kind))
        if len(files) >= _MAX_LISTED_FILES:
            break
    return WorkspaceListing(
        ready=True,
        run_id=status.run_id,
        root=str(root),
        files=files,
    )


def read_project_workspace_file(
    db: Session, user: User, project_id: int, relative_path: str
) -> WorkspaceFileContent:
    project_service.get_user_project(db, user, project_id)
    status = get_requirements_status(db, user, project_id)
    if (
        status.run_id is None
        or not status.workspace_ready
        or not workspace_is_ready(project_id, status.run_id)
    ):
        raise ConflictException("应用工作区尚未准备好")
    root = default_workspace_path(settings.runtime_data_root, project_id, status.run_id)
    target = _safe_file_under_root(root, relative_path)
    if not target.is_file():
        raise NotFoundException("文件不存在")
    kind = _file_kind(target)
    if kind is None:
        raise ConflictException(f"{relative_path} 不是可预览的工作区文件")
    raw = target.read_bytes()
    normalized = relative_path.replace("\\", "/")
    if kind == "image":
        truncated = len(raw) > _MAX_IMAGE_BYTES
        payload = raw[:_MAX_IMAGE_BYTES]
        return WorkspaceFileContent(
            path=normalized,
            kind="image",
            content=base64.b64encode(payload).decode("ascii"),
            media_type=_IMAGE_MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream"),
            truncated=truncated,
            size_bytes=len(raw),
        )
    truncated = len(raw) > _MAX_TEXT_BYTES
    payload = raw[:_MAX_TEXT_BYTES]
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConflictException(f"{normalized} 不是可预览的文本文件") from exc
    return WorkspaceFileContent(
        path=normalized,
        kind="text",
        content=text,
        truncated=truncated,
        size_bytes=len(raw),
    )


def _safe_file_under_root(root: Path, relative_path: str) -> Path:
    cleaned = relative_path.replace("\\", "/").strip()
    if not cleaned or cleaned.startswith("/") or ".." in cleaned.split("/"):
        raise ConflictException("非法文件路径")
    if any(part in _SKIP_DIRS for part in cleaned.split("/")):
        raise ConflictException("不允许读取该路径")
    if cleaned.startswith("forgeai/"):
        raise ConflictException("不允许读取该路径")
    root_resolved = root.resolve()
    candidate = (root / cleaned).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ConflictException("非法文件路径") from exc
    return candidate
