"""Run generated previews in disposable, unprivileged Docker containers."""

from __future__ import annotations

import hashlib
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.core.exceptions import BusinessException
from app.core.settings import settings
from app.runtime.docker import get_docker_sandbox_runtime
from app.tools.checks import source_snapshot

PREVIEW_RUNTIME_LABEL = "org.forgeai.preview-runtime-version"
PREVIEW_RUNTIME_VERSION = "3"
_MAX_LOG_BYTES = 16 * 1024


@dataclass(frozen=True)
class PreviewSandboxRequest:
    project_id: int
    run_id: str
    workspace: Path
    bind_host: str
    host_port: int
    log_path: Path

    snapshot_bytes: bytes | None = None


@dataclass(frozen=True)
class PreviewSandboxHandle:
    sandbox_id: str
    source_hash: str
    host: str
    port: int
    log_path: Path

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class PreviewSandboxRuntime(Protocol):
    def start(self, request: PreviewSandboxRequest) -> PreviewSandboxHandle: ...

    def stop(self, handle: PreviewSandboxHandle) -> None: ...

    def is_running(self, handle: PreviewSandboxHandle) -> bool: ...

    def diagnostic_tail(self, handle: PreviewSandboxHandle) -> str: ...


def _loopback_host(value: str) -> str:
    host = value.strip().lower()
    if host in {"localhost", "127.0.0.1"}:
        return "127.0.0.1"
    raise BusinessException("隔离预览只能绑定到 127.0.0.1")


def _sandbox_id(project_id: int, run_id: str, source_hash: str) -> str:
    identity = hashlib.sha256(f"{project_id}:{run_id}:{source_hash}".encode()).hexdigest()[:20]
    return f"forgeai-preview-{identity}-{uuid4().hex[:8]}"


def preview_docker_command(
    binary: str,
    image: str,
    request: PreviewSandboxRequest,
    sandbox_id: str,
) -> list[str]:
    """Build the fixed Docker invocation; generated source is delivered only on stdin."""

    host = _loopback_host(request.bind_host)
    return [
        binary,
        "run",
        "--rm",
        "--pull=never",
        "--name",
        sandbox_id,
        "--hostname=generated-preview",
        *get_docker_sandbox_runtime().isolation_args(
            cpus=settings.preview_container_cpus,
            memory_mb=settings.preview_container_memory_mb,
            pids_limit=settings.preview_container_pids_limit,
            tmpfs="rw,exec,nosuid,nodev,size=1536m,mode=1777",
        ),
        "--publish",
        f"{host}:{request.host_port}:8080",
        "--env",
        "DEBUG=false",
        "--env",
        f"CORS_ORIGINS={settings.cors_origins}",
        "-i",
        "--entrypoint=python",
        image,
        "-I",
        "/opt/forgeai/preview.py",
    ]


class DockerPreviewRuntime:
    """Docker implementation with no host mounts and an explicit environment allow-list."""

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()

    def _binary_and_image(self) -> tuple[str, str]:
        image = settings.preview_runtime_image
        binary = get_docker_sandbox_runtime().require_image(
            image,
            label=PREVIEW_RUNTIME_LABEL,
            version=PREVIEW_RUNTIME_VERSION,
            purpose="隔离预览",
        )
        return binary, image

    def start(self, request: PreviewSandboxRequest) -> PreviewSandboxHandle:
        binary, image = self._binary_and_image()
        if request.snapshot_bytes is None:
            payload, source_hash = source_snapshot(request.workspace)
        else:
            payload = request.snapshot_bytes
            source_hash = hashlib.sha256(payload).hexdigest()
        sandbox_id = _sandbox_id(request.project_id, request.run_id, source_hash)
        handle = PreviewSandboxHandle(
            sandbox_id=sandbox_id,
            source_hash=source_hash,
            host=_loopback_host(request.bind_host),
            port=request.host_port,
            log_path=request.log_path,
        )
        get_docker_sandbox_runtime().remove_container(binary, sandbox_id)
        request.log_path.parent.mkdir(parents=True, exist_ok=True)
        command = preview_docker_command(binary, image, request, sandbox_id)
        try:
            with request.log_path.open("wb") as output:
                process = get_docker_sandbox_runtime().start_streaming(
                    command, payload=payload, output=output
                )
            with self._lock:
                self._processes[sandbox_id] = process
            if process.poll() is not None:
                raise BusinessException(self._failure_message(handle, "隔离预览容器启动失败"))
            return handle
        except BusinessException:
            self.stop(handle)
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            self.stop(handle)
            raise BusinessException(f"无法启动隔离预览容器：{str(exc)[:400]}") from exc

    def stop(self, handle: PreviewSandboxHandle) -> None:
        try:
            binary = get_docker_sandbox_runtime().require_daemon(purpose="隔离预览")
        except BusinessException:
            binary = None
        if binary is not None:
            get_docker_sandbox_runtime().remove_container(binary, handle.sandbox_id)
        with self._lock:
            process = self._processes.pop(handle.sandbox_id, None)
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def is_running(self, handle: PreviewSandboxHandle) -> bool:
        try:
            binary = get_docker_sandbox_runtime().require_daemon(purpose="隔离预览")
        except BusinessException:
            return False
        return get_docker_sandbox_runtime().is_running(binary, handle.sandbox_id)

    def diagnostic_tail(self, handle: PreviewSandboxHandle) -> str:
        try:
            raw = handle.log_path.read_bytes()[-(_MAX_LOG_BYTES * 4) :]
        except OSError:
            return ""
        return raw.decode("utf-8", errors="replace").strip()[-_MAX_LOG_BYTES:]

    def _failure_message(self, handle: PreviewSandboxHandle, summary: str) -> str:
        detail = self.diagnostic_tail(handle)
        return summary if not detail else f"{summary}：{detail[-2000:]}"


_runtime = DockerPreviewRuntime()


def get_preview_runtime() -> PreviewSandboxRuntime:
    return _runtime
