"""Local loopback preview sessions for completed generated apps."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
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
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.core.settings import settings
from app.generation.workspace import default_workspace_path, workspace_is_ready
from app.models.user import User
from app.services import project as project_service
from app.services.requirements import get_requirements_status

logger = logging.getLogger("forgeai.preview")

PreviewState = Literal["idle", "starting", "ready", "error", "disabled"]


class PreviewStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PreviewState
    url: str | None = None
    run_id: str | None = None
    message: str | None = None


@dataclass
class _PreviewSession:
    project_id: int
    run_id: str
    workspace: Path
    status: PreviewState = "starting"
    url: str | None = None
    message: str | None = None
    backend_port: int | None = None
    preview_port: int | None = None
    backend_proc: subprocess.Popen[bytes] | None = None
    proxy_server: ThreadingHTTPServer | None = None
    proxy_thread: threading.Thread | None = None
    last_access: float = field(default_factory=time.monotonic)
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
            message=session.message,
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
    if status.state != "completed" or not status.run_id:
        raise BusinessException("只有独立验收通过的构建才能启动预览")
    run_id = status.run_id
    if not workspace_is_ready(project_id, run_id):
        raise BusinessException("工作区尚未就绪，无法启动预览")
    workspace = default_workspace_path(settings.runtime_data_root, project_id, run_id)
    if not workspace.is_dir():
        raise BusinessException("工作区目录不存在")

    _ensure_sweeper()
    with _sessions_lock:
        existing = _sessions.get(project_id)
        if existing is not None:
            with existing.lock:
                if existing.run_id == run_id and existing.status in {"starting", "ready"}:
                    _touch(existing)
                    return PreviewStatus(
                        status=existing.status,
                        url=existing.url,
                        run_id=existing.run_id,
                        message=existing.message,
                    )
            _stop_session_locked(existing)
            _sessions.pop(project_id, None)

        session = _PreviewSession(project_id=project_id, run_id=run_id, workspace=workspace)
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
        proc = session.backend_proc
        backend_port = session.backend_port
        preview_port = session.preview_port
        session.proxy_server = None
        session.backend_proc = None
        session.backend_port = None
        session.preview_port = None
        session.status = "idle"
        session.url = None
    if server is not None:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("preview proxy shutdown failed project_id=%s", session.project_id)
    if proc is not None:
        _stop_process(proc)
    _release_ports(backend_port, preview_port)


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _backend_env(workspace: Path) -> dict[str, str]:
    db_path = (workspace / "backend" / "data" / "app.db").resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    env["DEBUG"] = "false"
    # Allow embedding from the ForgeAI workbench origin.
    env["CORS_ORIGINS"] = settings.cors_origins
    return env


def _run_checked(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    label: str,
) -> None:
    logger.info("preview %s: %s", label, " ".join(argv))
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode == 0:
        return
    tail = (completed.stderr or completed.stdout or "").strip()[-800:]
    raise BusinessException(f"{label}失败" + (f"：{tail}" if tail else ""))


def _ensure_frontend_dist(workspace: Path) -> None:
    dist_index = workspace / "frontend" / "dist" / "index.html"
    if dist_index.is_file():
        return
    vite_entry = workspace / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite_entry.is_file():
        raise BusinessException("前端依赖未安装，无法构建预览（缺少 vite）")
    _run_checked(
        ["node", str(vite_entry), "build"],
        cwd=workspace / "frontend",
        env=dict(os.environ),
        timeout=int(settings.preview_build_timeout_seconds),
        label="前端构建",
    )
    if not dist_index.is_file():
        raise BusinessException("前端构建未生成 dist/index.html")


def _ensure_backend_ready(workspace: Path, env: dict[str, str]) -> None:
    backend = workspace / "backend"
    if not (backend / "pyproject.toml").is_file():
        raise BusinessException("工作区缺少后端工程")
    # Sync deps when the app venv is missing; reuse when present.
    if not (backend / ".venv").is_dir():
        _run_checked(
            ["uv", "sync"],
            cwd=backend,
            env=env,
            timeout=int(settings.preview_build_timeout_seconds),
            label="后端依赖同步",
        )
    _run_checked(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=backend,
        env=env,
        timeout=60,
        label="数据库迁移",
    )


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


def _make_proxy_handler(dist_root: Path, backend_port: int, bind_host: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            logger.debug("preview-proxy: " + format, *args)

        def _backend_base(self) -> str:
            return f"http://{bind_host}:{backend_port}"

        def _proxy(self) -> None:
            split = urlsplit(self.path)
            target = f"{self._backend_base()}{split.path}"
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
                    if key.lower() not in {"host", "content-length", "connection"}
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read()
                    self.send_response(response.status)
                    for key, value in response.headers.items():
                        if key.lower() in {"transfer-encoding", "connection", "content-encoding"}:
                            continue
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                self.send_response(exc.code)
                self.send_header("Content-Type", exc.headers.get("Content-Type", "text/plain"))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as exc:  # noqa: BLE001
                message = str(exc).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(message)))
                self.end_headers()
                self.wfile.write(message)

        def _safe_file(self, url_path: str) -> Path | None:
            relative = url_path.lstrip("/")
            if not relative or relative.endswith("/"):
                relative = relative + "index.html"
            candidate = (dist_root / relative).resolve()
            try:
                candidate.relative_to(dist_root.resolve())
            except ValueError:
                return None
            if candidate.is_file():
                return candidate
            # SPA fallback
            index = dist_root / "index.html"
            return index if index.is_file() else None

        def _serve_static(self) -> None:
            split = urlsplit(self.path)
            path = split.path or "/"
            if path.startswith("/api"):
                self._proxy()
                return
            file_path = self._safe_file(path)
            if file_path is None:
                self.send_error(404, "Not Found")
                return
            data = file_path.read_bytes()
            content_type = "text/html; charset=utf-8"
            suffix = file_path.suffix.lower()
            if suffix == ".js":
                content_type = "text/javascript; charset=utf-8"
            elif suffix == ".css":
                content_type = "text/css; charset=utf-8"
            elif suffix == ".json":
                content_type = "application/json"
            elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg"}:
                content_type = {
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".gif": "image/gif",
                    ".webp": "image/webp",
                    ".ico": "image/x-icon",
                    ".svg": "image/svg+xml",
                }[suffix]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._serve_static()

        def do_HEAD(self) -> None:  # noqa: N802
            self._serve_static()

        def do_POST(self) -> None:  # noqa: N802
            if urlsplit(self.path).path.startswith("/api"):
                self._proxy()
            else:
                self.send_error(405, "Method Not Allowed")

        def do_PUT(self) -> None:  # noqa: N802
            self.do_POST()

        def do_PATCH(self) -> None:  # noqa: N802
            self.do_POST()

        def do_DELETE(self) -> None:  # noqa: N802
            self.do_POST()

    return Handler


def _start_proxy(
    dist_root: Path,
    bind_host: str,
    preview_port: int,
    backend_port: int,
) -> ThreadingHTTPServer:
    handler = _make_proxy_handler(dist_root, backend_port, bind_host)
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
    try:
        with session.lock:
            session.status = "starting"
            session.message = "正在准备预览环境…"
            _touch(session)
        env = _backend_env(session.workspace)
        _ensure_frontend_dist(session.workspace)
        with session.lock:
            session.message = "正在准备后端…"
            _touch(session)
        _ensure_backend_ready(session.workspace, env)
        backend_port = _allocate_port(bind_host)
        preview_port = _allocate_port(bind_host)
        with session.lock:
            session.backend_port = backend_port
            session.preview_port = preview_port
            session.message = "正在启动应用…"
            _touch(session)
        backend_proc = subprocess.Popen(
            [
                "uv",
                "run",
                "uvicorn",
                "app.main:app",
                "--host",
                bind_host,
                "--port",
                str(backend_port),
            ],
            cwd=session.workspace / "backend",
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with session.lock:
            session.backend_proc = backend_proc
        _wait_http(
            f"http://{bind_host}:{backend_port}/api/v1/health",
            timeout=float(settings.preview_ready_timeout_seconds),
            label="预览后端",
        )
        dist_root = session.workspace / "frontend" / "dist"
        proxy = _start_proxy(dist_root, bind_host, preview_port, backend_port)
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
            if backend_proc.poll() is not None:
                raise BusinessException("预览后端进程已退出")
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
        logger.exception("preview boot failed project_id=%s", session.project_id)
        with session.lock:
            proc = session.backend_proc
            server = session.proxy_server
            backend_port = session.backend_port
            preview_port = session.preview_port
            session.backend_proc = None
            session.proxy_server = None
            session.backend_port = None
            session.preview_port = None
            session.status = "error"
            session.message = message[:500]
            session.url = None
            _touch(session)
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
        if proc is not None:
            _stop_process(proc)
        _release_ports(backend_port, preview_port)
