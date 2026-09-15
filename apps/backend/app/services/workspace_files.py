from __future__ import annotations

from pathlib import Path

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
_MAX_FILE_BYTES = 512 * 1024
_MAX_LISTED_FILES = 400


class WorkspaceFileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=0)


class WorkspaceListing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    run_id: str | None = None
    root: str | None = None
    files: list[WorkspaceFileEntry] = Field(default_factory=list)


class WorkspaceFileContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    truncated: bool = False
    size_bytes: int = Field(ge=0)


def list_project_workspace(db: Session, user: User, project_id: int) -> WorkspaceListing:
    project_service.get_user_project(db, user, project_id)
    status = get_requirements_status(db, user, project_id)
    if (
        status.run_id is None
        or not status.code_ready
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
        suffix = path.suffix.lower()
        if suffix not in _TEXT_SUFFIXES and path.name not in {"README", "LICENSE", ".env.example"}:
            continue
        files.append(WorkspaceFileEntry(path=relative, size_bytes=path.stat().st_size))
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
        or not status.code_ready
        or not workspace_is_ready(project_id, status.run_id)
    ):
        raise ConflictException("应用代码尚未生成")
    root = default_workspace_path(settings.runtime_data_root, project_id, status.run_id)
    target = _safe_file_under_root(root, relative_path)
    if not target.is_file():
        raise NotFoundException("文件不存在")
    raw = target.read_bytes()
    truncated = len(raw) > _MAX_FILE_BYTES
    payload = raw[:_MAX_FILE_BYTES]
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConflictException("该文件不是可预览的文本文件") from exc
    return WorkspaceFileContent(
        path=relative_path.replace("\\", "/"),
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
