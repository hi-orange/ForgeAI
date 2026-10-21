from __future__ import annotations

import base64
import binascii
import json
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException, NotFoundException
from app.models.user import User
from app.models.workflow_event import WorkflowEvent
from app.services import project as project_service
from app.services.requirements import get_requirements_status
from app.services.source_artifact import ensure_code_workspace, read_source_artifact_snapshot
from app.services.workspace_documents import run_documents
from app.tools.checks import source_snapshot

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

WorkspaceFileKind = Literal["text", "image", "document"]


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
    run_id: str | None = None
    kind: WorkspaceFileKind = "text"
    content: str
    media_type: str | None = None
    truncated: bool = False
    size_bytes: int = Field(ge=0)


def _file_kind(path: PurePosixPath) -> WorkspaceFileKind | None:
    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES or path.name in {"README", "LICENSE", ".env.example"}:
        return "text"
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    return None


def _decode_visible_snapshot(payload: bytes) -> dict[str, tuple[WorkspaceFileKind, bytes]]:
    try:
        raw_files = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConflictException("源码 revision 快照格式损坏") from exc
    if not isinstance(raw_files, dict) or len(raw_files) > _MAX_LISTED_FILES:
        raise ConflictException("源码 revision 文件清单无效")

    files: dict[str, tuple[WorkspaceFileKind, bytes]] = {}
    text_size = 0
    image_size = 0
    for relative, encoded in raw_files.items():
        if not isinstance(relative, str):
            raise ConflictException("源码 revision 包含非法路径")
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or not path.parts
            or "\\" in relative
            or ".." in path.parts
            or path.parts[0] == "forgeai"
            or path.name.startswith(".env")
            or any(part in _SKIP_DIRS for part in path.parts)
        ):
            raise ConflictException("源码 revision 包含非法路径")
        kind = _file_kind(path)
        if kind is None:
            continue
        if isinstance(encoded, str):
            if kind != "text":
                raise ConflictException("源码 revision 文件编码无效")
            data = encoded.encode("utf-8")
            text_size += len(data)
            if len(data) > _MAX_TEXT_BYTES or text_size > _MAX_TEXT_BYTES * 20:
                raise ConflictException("源码 revision 文本超过展示上限")
        elif (
            kind == "image"
            and len(path.parts) >= 3
            and path.parts[:2] == ("frontend", "public")
            and isinstance(encoded, dict)
            and encoded.get("encoding") == "base64"
            and isinstance(encoded.get("content"), str)
        ):
            try:
                data = base64.b64decode(encoded["content"], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ConflictException("源码 revision 图片编码损坏") from exc
            image_size += len(data)
            if len(data) > _MAX_IMAGE_BYTES or image_size > _MAX_IMAGE_BYTES * 20:
                raise ConflictException("源码 revision 图片超过展示上限")
        else:
            raise ConflictException("源码 revision 文件编码无效")
        files[relative] = (kind, data)
    return files


def _visible_source_snapshot(
    db: Session,
    *,
    project_id: int,
    status: object,
) -> tuple[str, dict[str, tuple[WorkspaceFileKind, bytes]]] | None:
    run_id = getattr(status, "run_id", None)
    if not isinstance(run_id, str):
        return None
    revision = getattr(status, "preview_revision", None)
    source_hash = getattr(status, "preview_source_hash", None)
    if revision is not None:
        if not isinstance(revision, int) or not isinstance(source_hash, str):
            raise ConflictException("源码 revision 身份不完整")
        event = db.scalar(
            select(WorkflowEvent).where(
                WorkflowEvent.project_id == project_id,
                WorkflowEvent.build_run_id == run_id,
                WorkflowEvent.stream_key == f"preview:{run_id}",
                WorkflowEvent.event_type == "preview_revision_published",
                WorkflowEvent.sequence == revision,
            )
        )
        payload = event.payload if event is not None and isinstance(event.payload, dict) else None
        artifact_id = payload.get("source_artifact_id") if payload else None
        event_hash = payload.get("source_hash") if payload else None
        if not isinstance(artifact_id, str) or event_hash != source_hash:
            raise ConflictException("源码 revision 身份不完整")
        snapshot = read_source_artifact_snapshot(
            db,
            project_id=project_id,
            artifact_id=artifact_id,
            source_hash=source_hash,
        )
        return run_id, _decode_visible_snapshot(snapshot)

    code_item_id = getattr(status, "code_item_id", None)
    if not isinstance(code_item_id, str):
        return None
    root, restored = ensure_code_workspace(
        db,
        project_id=project_id,
        run_id=run_id,
        code_item_id=code_item_id,
    )
    if restored:
        db.commit()
    snapshot, _ = source_snapshot(root)
    return run_id, _decode_visible_snapshot(snapshot)


def list_project_workspace(
    db: Session,
    user: User,
    project_id: int,
    run_id: str | None = None,
) -> WorkspaceListing:
    project_service.get_user_project(db, user, project_id)
    status = get_requirements_status(db, user, project_id)
    if run_id is not None and status.run_id != run_id:
        raise ConflictException("构建已切换，请刷新文件列表")
    documents = run_documents(db, project_id, status)
    visible = _visible_source_snapshot(db, project_id=project_id, status=status)
    if visible is None and not documents:
        return WorkspaceListing(ready=False, run_id=status.run_id)
    snapshot = visible[1] if visible else {}
    files = [
        WorkspaceFileEntry(path=path, size_bytes=len(data), kind="document")
        for path, data in sorted(documents.items())
    ] + [
        WorkspaceFileEntry(path=path, size_bytes=len(data), kind=kind)
        for path, (kind, data) in sorted(snapshot.items())
    ]
    return WorkspaceListing(
        ready=True,
        run_id=status.run_id,
        files=files,
    )


def read_project_workspace_file(
    db: Session,
    user: User,
    project_id: int,
    relative_path: str,
    run_id: str | None = None,
) -> WorkspaceFileContent:
    project_service.get_user_project(db, user, project_id)
    status = get_requirements_status(db, user, project_id)
    if run_id is not None and status.run_id != run_id:
        raise ConflictException("构建已切换，请刷新文件列表")
    normalized = relative_path.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ConflictException("非法文件路径")
    if path.parts[0] == "forgeai":
        document = run_documents(db, project_id, status).get(normalized)
        if document is None:
            raise NotFoundException("文档尚未生成或不存在")
        return WorkspaceFileContent(
            path=normalized,
            run_id=status.run_id,
            kind="document",
            content=document[:_MAX_TEXT_BYTES].decode("utf-8", errors="ignore"),
            media_type="text/markdown",
            size_bytes=len(document),
            truncated=len(document) > _MAX_TEXT_BYTES,
        )
    visible = _visible_source_snapshot(db, project_id=project_id, status=status)
    if visible is None:
        raise ConflictException("源码 revision 尚未生成")
    _, snapshot = visible
    source = snapshot.get(normalized)
    if source is None:
        raise NotFoundException("文件不存在")
    kind, raw = source
    if kind == "image":
        truncated = len(raw) > _MAX_IMAGE_BYTES
        payload = raw[:_MAX_IMAGE_BYTES]
        return WorkspaceFileContent(
            path=normalized,
            run_id=status.run_id,
            kind="image",
            content=base64.b64encode(payload).decode("ascii"),
            media_type=_IMAGE_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
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
        run_id=status.run_id,
        kind="text",
        content=text,
        truncated=truncated,
        size_bytes=len(raw),
    )
