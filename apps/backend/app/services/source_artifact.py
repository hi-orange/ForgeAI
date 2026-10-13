from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.artifacts.source import (
    SOURCE_ARTIFACT_FORMAT,
    SOURCE_ARTIFACT_PROVIDER,
    FilesystemSourceArtifactStore,
)
from app.core.exceptions import ConflictException, NotFoundException
from app.core.settings import settings
from app.generation.workspace import default_workspace_path, publish_workspace_directory
from app.models.configuration_item import ConfigurationItem, ConfigurationItemType
from app.models.source_artifact import SourceArtifact
from app.schemas.code_artifact import CodeArtifact, SourceFileIdentity
from app.services.workflow_events import append_event
from app.tools.checks import (
    MAX_BINARY_ASSET_BYTES,
    MAX_BINARY_ASSETS_BYTES,
    MAX_SOURCE_BYTES,
    source_snapshot,
)
from app.tools.paths import MAX_FILE_BYTES


def _store() -> FilesystemSourceArtifactStore:
    return FilesystemSourceArtifactStore(settings.source_artifact_root)


def persist_source_artifact(
    db: Session,
    *,
    project_id: int,
    snapshot_bytes: bytes,
    source_hash: str,
) -> SourceArtifact:
    """Persist bytes before publishing the SQL pointer; safe to replay by hash."""

    stored = _store().put(project_id, snapshot_bytes, source_hash)
    existing = db.scalar(
        select(SourceArtifact).where(
            SourceArtifact.project_id == project_id,
            SourceArtifact.source_hash == source_hash,
        )
    )
    if existing is not None:
        _assert_metadata(existing, stored.object_key, stored.size_bytes)
        return existing
    artifact = SourceArtifact(
        artifact_id=f"src_{uuid4().hex}",
        project_id=project_id,
        source_hash=source_hash,
        storage_provider=stored.provider,
        object_key=stored.object_key,
        size_bytes=stored.size_bytes,
        artifact_format=stored.artifact_format,
    )
    try:
        with db.begin_nested():
            db.add(artifact)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(SourceArtifact).where(
                SourceArtifact.project_id == project_id,
                SourceArtifact.source_hash == source_hash,
            )
        )
        if existing is None:
            raise
        _assert_metadata(existing, stored.object_key, stored.size_bytes)
        return existing
    return artifact


def read_source_artifact_snapshot(
    db: Session,
    *,
    project_id: int,
    artifact_id: str,
    source_hash: str,
) -> bytes:
    """Read one immutable snapshot by its exact preview/code identity."""

    artifact = db.scalar(
        select(SourceArtifact).where(
            SourceArtifact.artifact_id == artifact_id,
            SourceArtifact.project_id == project_id,
            SourceArtifact.source_hash == source_hash,
        )
    )
    if artifact is None:
        raise NotFoundException("源码 revision 对象不存在")
    if (
        artifact.storage_provider != SOURCE_ARTIFACT_PROVIDER
        or artifact.artifact_format != SOURCE_ARTIFACT_FORMAT
    ):
        raise ConflictException("源码 revision 存储格式不受支持")
    return _store().read(
        artifact.object_key,
        source_hash=artifact.source_hash,
        size_bytes=artifact.size_bytes,
    )


def _assert_metadata(artifact: SourceArtifact, object_key: str, size_bytes: int) -> None:
    if (
        artifact.storage_provider != SOURCE_ARTIFACT_PROVIDER
        or artifact.artifact_format != SOURCE_ARTIFACT_FORMAT
        or artifact.object_key != object_key
        or artifact.size_bytes != size_bytes
    ):
        raise ConflictException("源码 artifact 元数据冲突")


def _load_code_artifact(code_item: ConfigurationItem) -> CodeArtifact:
    if code_item.semantic_type != ConfigurationItemType.CODE.value:
        raise ConflictException("指定成果不是 code artifact")
    try:
        value = CodeArtifact.model_validate(code_item.payload)
    except ValidationError as exc:
        raise ConflictException("code artifact 结构无效") from exc
    return value


def _load_payload(
    db: Session,
    *,
    project_id: int,
    code_item: ConfigurationItem,
) -> tuple[CodeArtifact, bytes]:
    value = _load_code_artifact(code_item)
    if value.source_artifact_id:
        artifact = db.scalar(
            select(SourceArtifact).where(
                SourceArtifact.artifact_id == value.source_artifact_id,
                SourceArtifact.project_id == project_id,
            )
        )
    else:
        # Schema-v1 ConfigurationItems stay immutable. Once their still-live
        # workspace has been lazily persisted, hash lookup provides the same
        # exact recovery identity without rewriting historical payloads.
        artifact = db.scalar(
            select(SourceArtifact).where(
                SourceArtifact.project_id == project_id,
                SourceArtifact.source_hash == value.source_hash,
            )
        )
    if artifact is None:
        if value.source_artifact_id:
            raise NotFoundException("code artifact 引用的源码对象不存在")
        raise ConflictException("旧版 code artifact 尚未固化源码，无法灾难恢复")
    if (
        artifact.source_hash != value.source_hash
        or (
            value.artifact_size_bytes is not None
            and artifact.size_bytes != value.artifact_size_bytes
        )
        or (value.artifact_format is not None and artifact.artifact_format != value.artifact_format)
        or artifact.artifact_format != SOURCE_ARTIFACT_FORMAT
        or artifact.storage_provider != SOURCE_ARTIFACT_PROVIDER
    ):
        raise ConflictException("code artifact 与源码对象身份不一致")
    payload = _store().read(
        artifact.object_key,
        source_hash=artifact.source_hash,
        size_bytes=artifact.size_bytes,
    )
    return value, payload


def _decode_snapshot(
    payload: bytes,
    manifest: list[SourceFileIdentity],
) -> dict[str, bytes]:
    try:
        raw_files = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConflictException("源码 artifact 快照格式损坏") from exc
    if not isinstance(raw_files, dict) or not raw_files or len(raw_files) > 400:
        raise ConflictException("源码 artifact 文件清单无效")
    identities = {item.path: item for item in manifest}
    if len(identities) != len(manifest) or set(raw_files) != set(identities):
        raise ConflictException("源码 artifact 文件清单与 code artifact 不一致")
    decoded: dict[str, bytes] = {}
    text_size = 0
    binary_size = 0
    for relative, content in raw_files.items():
        if not isinstance(relative, str):
            raise ConflictException("源码 artifact 包含非法路径")
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or not path.parts
            or "\\" in relative
            or ".." in path.parts
            or path.parts[0] == "forgeai"
            or path.name.startswith(".env")
        ):
            raise ConflictException("源码 artifact 包含非法路径")
        if isinstance(content, str):
            data = content.encode("utf-8")
            text_size += len(data)
            if len(data) > MAX_FILE_BYTES or text_size > MAX_SOURCE_BYTES:
                raise ConflictException("源码 artifact 文本超过恢复上限")
        elif (
            isinstance(content, dict)
            and content.get("encoding") == "base64"
            and isinstance(content.get("content"), str)
        ):
            try:
                data = base64.b64decode(content["content"], validate=True)
            except ValueError as exc:
                raise ConflictException("源码 artifact 二进制内容损坏") from exc
            binary_size += len(data)
            if len(data) > MAX_BINARY_ASSET_BYTES or binary_size > MAX_BINARY_ASSETS_BYTES:
                raise ConflictException("源码 artifact 图片超过恢复上限")
        else:
            raise ConflictException("源码 artifact 内容编码无效")
        identity = identities[relative]
        if len(data) != identity.size_bytes or hashlib.sha256(data).hexdigest() != identity.sha256:
            raise ConflictException("源码 artifact 文件完整性校验失败")
        decoded[relative] = data
    return decoded


def ensure_code_workspace(
    db: Session,
    *,
    project_id: int,
    run_id: str,
    code_item_id: str,
    runtime_data_root: str | Path | None = None,
) -> tuple[Path, bool]:
    """Return the exact workspace, restoring it from its immutable artifact if absent."""

    code_item = db.scalar(
        select(ConfigurationItem).where(
            ConfigurationItem.item_id == code_item_id,
            ConfigurationItem.project_id == project_id,
            ConfigurationItem.producer_run_id == run_id,
        )
    )
    if code_item is None:
        raise NotFoundException("code artifact 不存在或不属于指定构建")
    value = _load_code_artifact(code_item)
    root = runtime_data_root if runtime_data_root is not None else settings.runtime_data_root
    destination = default_workspace_path(root, project_id, run_id)
    if destination.exists():
        current_payload, current_hash = source_snapshot(destination)
        if current_hash != value.source_hash:
            raise ConflictException("现有工作区与已发布 code artifact 不一致，拒绝覆盖")
        backfilled = value.source_artifact_id is None
        persist_source_artifact(
            db,
            project_id=project_id,
            snapshot_bytes=current_payload,
            source_hash=current_hash,
        )
        return destination, backfilled

    value, payload = _load_payload(db, project_id=project_id, code_item=code_item)
    files = _decode_snapshot(payload, value.files)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.restore-{uuid4().hex}"
    try:
        staging.mkdir()
        for relative, data in files.items():
            target = staging.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        _, restored_hash = source_snapshot(staging)
        if restored_hash != value.source_hash:
            raise ConflictException("恢复后的工作区未通过 source_hash 校验")
        try:
            publish_workspace_directory(staging, destination)
        except OSError:
            if not destination.exists():
                raise
            _, winner_hash = source_snapshot(destination)
            if winner_hash != value.source_hash:
                raise ConflictException("并发恢复产生了不一致工作区") from None
        append_event(
            db,
            idempotency_key=f"source-restore:{value.source_artifact_id}:{uuid4().hex}",
            stream_key=f"source-artifact:{value.source_artifact_id}",
            event_type="source_artifact_restored",
            project_id=project_id,
            build_run_id=run_id,
            payload={
                "code_item_id": code_item_id,
                "source_hash": value.source_hash,
                "destination": str(destination),
            },
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    return destination, True
