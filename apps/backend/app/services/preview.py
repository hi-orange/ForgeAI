"""Local loopback preview sessions for approved generated-app workspaces."""

from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.core.settings import settings
from app.generation.workspace import default_workspace_path, workspace_is_ready
from app.models.user import User
from app.models.workflow_event import WorkflowEvent
from app.runtime.preview import (
    PreviewSandboxHandle,
    PreviewSandboxRequest,
    get_preview_runtime,
)
from app.services import project as project_service
from app.services.preview_design import inject_design_bridge
from app.services.requirements import get_requirements_status
from app.services.source_artifact import (
    ensure_code_workspace,
    read_source_artifact_snapshot,
)
from app.tools.checks import source_snapshot

logger = logging.getLogger("forgeai.preview")

PreviewState = Literal["idle", "starting", "ready", "error", "disabled"]
_MAX_RUNTIME_DIAGNOSTIC_CHARS = 4_000


class PreviewStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PreviewState
    url: str | None = None
    run_id: str | None = None
    message: str | None = None
    verified: bool = False
    source_hash: str | None = None

    revision: int | None = None


@dataclass
class _PreviewSession:
    project_id: int
    run_id: str
    workspace: Path
    status: PreviewState = "starting"
    source_hash: str | None = None
    revision: int | None = None
    snapshot_bytes: bytes | None = None
    url: str | None = None
    message: str | None = None
    runtime_port: int | None = None
    preview_port: int | None = None
    runtime_handle: PreviewSandboxHandle | None = None
    preview_log_path: Path | None = None
    backend_error_path: str | None = None
    proxy_server: ThreadingHTTPServer | None = None
    proxy_thread: threading.Thread | None = None
    last_access: float = field(default_factory=time.monotonic)
    verified: bool = False
    stopped: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_sessions: dict[int, _PreviewSession] = {}
_sessions_lock = threading.Lock()
_reserved_ports: set[int] = set()
_sweeper_started = False


def _ensure_sweeper() -> None:
    global _sweeper_started
    if _sweeper_started:
        return
    _sweeper_started = True

    def sweep() -> None:
        while True:
            time.sleep(30)
            try:
                _reap_idle_sessions()
            except Exception:  # noqa: BLE001 - sweeper must not die
                logger.exception("preview idle sweeper failed")

    threading.Thread(target=sweep, name="forgeai-preview-sweep", daemon=True).start()


def _reap_idle_sessions() -> None:
    timeout = max(60, int(settings.preview_idle_timeout_seconds))
    now = time.monotonic()
    with _sessions_lock:
        stale = [
            project_id
            for project_id, session in _sessions.items()
            if session.status in {"ready", "error", "starting"}
            and now - session.last_access > timeout
        ]
    for project_id in stale:
        stop_preview(project_id)


def _release_ports(*ports: int | None) -> None:
    for port in ports:
        if port is not None:
            _reserved_ports.discard(port)


def _allocate_port(host: str) -> int:
    """Pick a free loopback port not already reserved by this process.

    Windows often forbids specific ports (WinError 10013) even when they look free.
    Probe the configured range first, then fall back to an OS-assigned ephemeral port.
    """

    def _try_bind(port: int) -> int | None:
        if port in _reserved_ports:
            return None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                # Do not set SO_REUSEADDR: on Windows it can mask exclusions.
                sock.bind((host, port))
            except PermissionError:
                return None
            except OSError as exc:
                if getattr(exc, "winerror", None) == 10013 or getattr(exc, "errno", None) in {
                    13,
                    10013,
                }:
                    return None
                return None
            chosen = int(sock.getsockname()[1])
        if chosen in _reserved_ports:
            return None
        _reserved_ports.add(chosen)
        return chosen

    low = int(settings.preview_port_min)
    high = int(settings.preview_port_max)
    if low <= high:
        for port in range(low, high + 1):
            chosen = _try_bind(port)
            if chosen is not None:
                return chosen
    chosen = _try_bind(0)
    if chosen is not None:
        return chosen
    raise BusinessException("无法分配预览端口")


def _touch(session: _PreviewSession) -> None:
    session.last_access = time.monotonic()


def _status_from_session(session: _PreviewSession | None) -> PreviewStatus:
    if session is None:
        return PreviewStatus(status="idle")
    with session.lock:
        _touch(session)
        return PreviewStatus(
            status=session.status,
            url=session.url,
            run_id=session.run_id,
            verified=session.verified,
            source_hash=(
                session.runtime_handle.source_hash
                if session.runtime_handle
                else session.source_hash
            ),
            message=session.message,
            revision=session.revision,
        )


def get_preview_status(db: Session, user: User, project_id: int) -> PreviewStatus:
    project_service.get_user_project(db, user, project_id)
    if not settings.preview_enabled:
        return PreviewStatus(status="disabled", message="本地预览未启用")
    with _sessions_lock:
        session = _sessions.get(project_id)
    return _status_from_session(session)


def start_preview(db: Session, user: User, project_id: int) -> PreviewStatus:
    project_service.get_user_project(db, user, project_id)
    if not settings.preview_enabled:
        return PreviewStatus(status="disabled", message="本地预览未启用")

    status = get_requirements_status(db, user, project_id)
    if not status.run_id or not status.workspace_ready:
        raise BusinessException("批准后的应用工作区尚未就绪，无法启动预览")
    if status.preview_revision is None and not status.code_item_id:
        raise BusinessException("首个源码 revision 尚未生成，无法启动预览")
    run_id = status.run_id
    verified = status.state == "completed"
    if status.code_item_id:
        _, restored = ensure_code_workspace(
            db,
            project_id=project_id,
            run_id=run_id,
            code_item_id=status.code_item_id,
        )
        if restored:
            db.commit()
    if not workspace_is_ready(project_id, run_id):
        raise BusinessException("工作区尚未就绪，无法启动预览")
    workspace = default_workspace_path(settings.runtime_data_root, project_id, run_id)
    if not workspace.is_dir():
        raise BusinessException("工作区目录不存在")

    snapshot_bytes: bytes | None = None
    revision = status.preview_revision
    if revision is not None and status.preview_source_hash is not None:
        revision_event = db.scalar(
            select(WorkflowEvent).where(
                WorkflowEvent.stream_key == f"preview:{run_id}",
                WorkflowEvent.event_type == "preview_revision_published",
                WorkflowEvent.sequence == revision,
            )
        )
        payload = revision_event.payload if revision_event is not None else None
        artifact_id = payload.get("source_artifact_id") if isinstance(payload, dict) else None
        event_hash = payload.get("source_hash") if isinstance(payload, dict) else None
        if not isinstance(artifact_id, str) or event_hash != status.preview_source_hash:
            raise BusinessException("预览 revision 身份不完整，请等待下一次快速检查")
        snapshot_bytes = read_source_artifact_snapshot(
            db,
            project_id=project_id,
            artifact_id=artifact_id,
            source_hash=status.preview_source_hash,
        )
        current_source_hash = status.preview_source_hash
    else:
        # Legacy/final code artifacts are identity-checked by ensure_code_workspace above.
        _, current_source_hash = source_snapshot(workspace)

    _ensure_sweeper()
    with _sessions_lock:
        existing = _sessions.get(project_id)
        if existing is not None:
            with existing.lock:
                existing_hash = (
                    existing.runtime_handle.source_hash
                    if existing.runtime_handle
                    else existing.source_hash
                )
                if (
                    existing.run_id == run_id
                    and existing.status == "starting"
                    and existing_hash == current_source_hash
                ) or (
                    existing.run_id == run_id
                    and existing.status == "ready"
                    and existing_hash == current_source_hash
                ):
                    existing.verified = verified
                    _touch(existing)
                    return PreviewStatus(
                        status=existing.status,
                        url=existing.url,
                        run_id=existing.run_id,
                        message=existing.message,
                        verified=existing.verified,
                        source_hash=existing_hash,
                        revision=existing.revision,
                    )
            _stop_session_locked(existing)
            _sessions.pop(project_id, None)

        session = _PreviewSession(
            project_id=project_id,
            run_id=run_id,
            workspace=workspace,
            source_hash=current_source_hash,
            revision=revision,
            snapshot_bytes=snapshot_bytes,
            verified=verified,
        )
        _sessions[project_id] = session

    worker = threading.Thread(
        target=_boot_session,
        args=(session,),
        name=f"forgeai-preview-{project_id}",
        daemon=True,
    )
    worker.start()
    return _status_from_session(session)


def stop_preview(project_id: int) -> PreviewStatus:
    with _sessions_lock:
        session = _sessions.pop(project_id, None)
    if session is None:
        return PreviewStatus(status="idle")
    _stop_session_locked(session)
    return PreviewStatus(status="idle", run_id=session.run_id)


def stop_preview_for_user(db: Session, user: User, project_id: int) -> PreviewStatus:
    project_service.get_user_project(db, user, project_id)
    return stop_preview(project_id)


def _stop_session_locked(session: _PreviewSession) -> None:
    with session.lock:
        server = session.proxy_server
        handle = session.runtime_handle
        runtime_port = session.runtime_port
        preview_port = session.preview_port
        worker_owns_pending_ports = session.status == "starting" and handle is None
        session.proxy_server = None
        session.runtime_handle = None
        if not worker_owns_pending_ports:
            session.runtime_port = None
            session.preview_port = None
        session.stopped = True
        session.status = "idle"
        session.url = None
    if server is not None:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("preview proxy shutdown failed project_id=%s", session.project_id)
    if handle is not None:
        try:
            get_preview_runtime().stop(handle)
        except Exception:  # noqa: BLE001 - stop remains best effort
            logger.exception("preview sandbox shutdown failed project_id=%s", session.project_id)
    if not worker_owns_pending_ports:
        _release_ports(runtime_port, preview_port)


def _preview_log_path(workspace: Path) -> Path:
    path = workspace / "forgeai" / "logs" / "preview-sandbox.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _preview_log_tail(session: _PreviewSession) -> str:
    path = session.preview_log_path
    if path is None or not path.is_file():
        return ""
    try:
        raw = path.read_bytes()[-(_MAX_RUNTIME_DIAGNOSTIC_CHARS * 4) :]
        text = raw.decode("utf-8", errors="replace").strip()
    except OSError:
        return ""
    workspace = str(session.workspace.resolve())
    return text.replace(workspace, "<workspace>").replace("/tmp/app", "<sandbox-workspace>")[
        -_MAX_RUNTIME_DIAGNOSTIC_CHARS:
    ]


def _with_backend_diagnostic(summary: str, detail: str) -> str:
    if not detail:
        return summary[:_MAX_RUNTIME_DIAGNOSTIC_CHARS]
    separator = "。后端日志："
    if len(summary) + len(separator) >= _MAX_RUNTIME_DIAGNOSTIC_CHARS:
        return summary[:_MAX_RUNTIME_DIAGNOSTIC_CHARS]
    remaining = max(0, _MAX_RUNTIME_DIAGNOSTIC_CHARS - len(summary) - len(separator))
    return f"{summary}{separator}{detail[-remaining:]}"


def _record_backend_failure(session: _PreviewSession, request_path: str, status: int) -> None:
    detail = _preview_log_tail(session)
    message = _with_backend_diagnostic(f"预览 API {request_path} 返回 HTTP {status}", detail)
    with session.lock:
        session.message = message
        session.backend_error_path = request_path
        _touch(session)


def _clear_backend_failure(session: _PreviewSession, request_path: str) -> None:
    with session.lock:
        if session.backend_error_path == request_path:
            session.message = None
            session.backend_error_path = None
        _touch(session)


def _wait_http(url: str, *, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 500:
                    return
                last_error = f"HTTP {response.status}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(0.4)
    raise BusinessException(f"{label}未在时限内就绪" + (f"：{last_error}" if last_error else ""))


def _make_proxy_handler(
    runtime_port: int,
    bind_host: str,
    workspace: Path,
    session: _PreviewSession,
):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            logger.debug("preview-proxy: " + format, *args)

        def _runtime_base(self) -> str:
            return f"http://{bind_host}:{runtime_port}"

        def _proxy(self) -> None:
            split = urlsplit(self.path)
            target = f"{self._runtime_base()}{split.path}"
            if split.query:
                target = f"{target}?{split.query}"
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else None
            request = urllib.request.Request(
                target,
                data=body,
                method=self.command,
                headers={
                    key: value
                    for key, value in self.headers.items()
                    if key.lower()
                    not in {"host", "content-length", "connection", "accept-encoding"}
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read()
                    content_type = response.headers.get("Content-Type", "")
                    if "text/html" in content_type.lower() and self.command != "HEAD":
                        payload = inject_design_bridge(payload, workspace)
                    if split.path.startswith("/api"):
                        _clear_backend_failure(session, split.path)
                    self.send_response(response.status)
                    for key, value in response.headers.items():
                        if key.lower() in {
                            "transfer-encoding",
                            "connection",
                            "content-encoding",
                            "content-length",
                        }:
                            continue
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(payload)
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                if split.path.startswith("/api") and exc.code >= 500:
                    _record_backend_failure(session, split.path, exc.code)
                self.send_response(exc.code)
                self.send_header("Content-Type", exc.headers.get("Content-Type", "text/plain"))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
            except Exception as exc:  # noqa: BLE001
                message = str(exc).encode("utf-8")
                if split.path.startswith("/api"):
                    _record_backend_failure(session, split.path, 502)
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(message)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(message)

        def do_GET(self) -> None:  # noqa: N802
            self._proxy()

        def do_HEAD(self) -> None:  # noqa: N802
            self._proxy()

        def do_POST(self) -> None:  # noqa: N802
            self._proxy()

        def do_PUT(self) -> None:  # noqa: N802
            self.do_POST()

        def do_PATCH(self) -> None:  # noqa: N802
            self.do_POST()

        def do_DELETE(self) -> None:  # noqa: N802
            self.do_POST()

    return Handler


def _start_proxy(
    bind_host: str,
    preview_port: int,
    runtime_port: int,
    workspace: Path,
    session: _PreviewSession,
) -> ThreadingHTTPServer:
    handler = _make_proxy_handler(runtime_port, bind_host, workspace, session)
    server = ThreadingHTTPServer((bind_host, preview_port), handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"forgeai-preview-http-{preview_port}",
        daemon=True,
    )
    thread.start()
    return server


def _boot_session(session: _PreviewSession) -> None:
    bind_host = settings.preview_bind_host.strip() or "127.0.0.1"
    runtime = get_preview_runtime()
    try:
        with session.lock:
            session.status = "starting"
            session.message = "正在启动隔离预览环境…"
            _touch(session)
        runtime_port = _allocate_port(bind_host)
        preview_port = _allocate_port(bind_host)
        with session.lock:
            session.runtime_port = runtime_port
            session.preview_port = preview_port
            session.message = "正在容器内准备依赖、迁移数据库并启动开发预览…"
            _touch(session)
            if session.stopped:
                session.runtime_port = None
                session.preview_port = None
                _release_ports(runtime_port, preview_port)
                return
        log_path = _preview_log_path(session.workspace)
        with session.lock:
            session.preview_log_path = log_path
        handle = runtime.start(
            PreviewSandboxRequest(
                project_id=session.project_id,
                run_id=session.run_id,
                workspace=session.workspace,
                bind_host=bind_host,
                host_port=runtime_port,
                log_path=log_path,
                snapshot_bytes=session.snapshot_bytes,
            )
        )
        with session.lock:
            session.runtime_handle = handle
            if session.stopped:
                session.runtime_handle = None
                session.runtime_port = None
                session.preview_port = None
                runtime.stop(handle)
                _release_ports(runtime_port, preview_port)
                return
        _wait_http(
            f"{handle.base_url}/api/v1/health",
            timeout=float(
                settings.preview_build_timeout_seconds + settings.preview_ready_timeout_seconds
            ),
            label="隔离预览容器",
        )
        proxy = _start_proxy(
            bind_host,
            preview_port,
            runtime_port,
            session.workspace,
            session,
        )
        with session.lock:
            session.proxy_server = proxy
        url = f"http://{bind_host}:{preview_port}/"
        _wait_http(url, timeout=15.0, label="预览页面")
        _wait_http(
            f"http://{bind_host}:{preview_port}/api/v1/health",
            timeout=15.0,
            label="预览 API 代理",
        )
        with session.lock:
            if session.stopped:
                raise BusinessException("预览已停止")
            if not runtime.is_running(handle):
                raise BusinessException("隔离预览容器已退出")
            session.url = url
            session.status = "ready"
            session.message = None
            _touch(session)
        logger.info(
            "preview ready project_id=%s run_id=%s url=%s",
            session.project_id,
            session.run_id,
            url,
        )
    except Exception as exc:
        message = str(exc) if str(exc).strip() else "预览启动失败"
        log_tail = _preview_log_tail(session)
        if log_tail and log_tail not in message:
            message = _with_backend_diagnostic(message, log_tail)
        logger.exception("preview boot failed project_id=%s", session.project_id)
        with session.lock:
            cleanup_handle = session.runtime_handle
            server = session.proxy_server
            released_runtime_port = session.runtime_port
            released_preview_port = session.preview_port
            stopped = session.stopped
            session.runtime_handle = None
            session.proxy_server = None
            session.runtime_port = None
            session.preview_port = None
            session.status = "idle" if stopped else "error"
            session.message = None if stopped else message[:_MAX_RUNTIME_DIAGNOSTIC_CHARS]
            session.url = None
            _touch(session)
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
        if cleanup_handle is not None:
            runtime.stop(cleanup_handle)
        _release_ports(released_runtime_port, released_preview_port)
