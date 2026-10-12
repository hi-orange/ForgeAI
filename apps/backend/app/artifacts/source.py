from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from app.core.exceptions import ConflictException

SOURCE_ARTIFACT_FORMAT = "canonical-json-v1"
SOURCE_ARTIFACT_PROVIDER = "filesystem"


@dataclass(frozen=True, slots=True)
class StoredSourceArtifact:
    provider: str
    object_key: str
    size_bytes: int
    artifact_format: str


class FilesystemSourceArtifactStore:
    """Atomic content-addressed storage outside disposable run workspaces."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    @staticmethod
    def object_key(project_id: int, source_hash: str) -> str:
        if project_id <= 0 or len(source_hash) != 64:
            raise ValueError("invalid source artifact identity")
        return f"projects/{project_id}/source/sha256/{source_hash[:2]}/{source_hash}.json"

    def _path(self, object_key: str) -> Path:
        pure = PurePosixPath(object_key)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            raise ConflictException("源码 artifact 对象路径非法")
        candidate = (self.root / Path(*pure.parts)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ConflictException("源码 artifact 对象路径越界") from exc
        current = self.root
        for part in pure.parts[:-1]:
            current = current / part
            if current.exists() and current.is_symlink():
                raise ConflictException("源码 artifact 存储路径包含符号链接")
        return candidate

    def put(self, project_id: int, payload: bytes, source_hash: str) -> StoredSourceArtifact:
        actual_hash = hashlib.sha256(payload).hexdigest()
        if actual_hash != source_hash:
            raise ConflictException("源码 artifact 内容与 source_hash 不一致")
        object_key = self.object_key(project_id, source_hash)
        target = self._path(object_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = target.read_bytes()
            if hashlib.sha256(existing).hexdigest() != source_hash:
                raise ConflictException("源码 artifact 对象已存在但内容损坏")
            return StoredSourceArtifact(
                provider=SOURCE_ARTIFACT_PROVIDER,
                object_key=object_key,
                size_bytes=len(existing),
                artifact_format=SOURCE_ARTIFACT_FORMAT,
            )
        temporary = target.parent / f".{target.name}.writing-{uuid4().hex}"
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.replace(temporary, target)
            except OSError:
                if not target.is_file():
                    raise
            stored = target.read_bytes()
            if hashlib.sha256(stored).hexdigest() != source_hash:
                raise ConflictException("源码 artifact 原子写入校验失败")
        finally:
            temporary.unlink(missing_ok=True)
        return StoredSourceArtifact(
            provider=SOURCE_ARTIFACT_PROVIDER,
            object_key=object_key,
            size_bytes=len(payload),
            artifact_format=SOURCE_ARTIFACT_FORMAT,
        )

    def read(self, object_key: str, *, source_hash: str, size_bytes: int) -> bytes:
        target = self._path(object_key)
        if not target.is_file() or target.is_symlink():
            raise ConflictException("源码 artifact 对象不存在")
        payload = target.read_bytes()
        if len(payload) != size_bytes or hashlib.sha256(payload).hexdigest() != source_hash:
            raise ConflictException("源码 artifact 完整性校验失败")
        return payload
