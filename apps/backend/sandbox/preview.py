"""Trusted persistent preview controller baked into the sandbox image.

Generated files arrive as a bounded JSON snapshot on stdin.  They are never
mounted from the host and are executed only inside this container.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path("/tmp/app")
BACKEND_BASE = "http://127.0.0.1:8000"
FRONTEND_BASE = "http://127.0.0.1:5173"


def _load_check_controller():
    spec = importlib.util.spec_from_file_location("forgeai_trusted_check", "/opt/forgeai/check.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("隔离预览控制器缺少 check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load_check_controller()


class PreviewHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        print("preview-http:", format % args, flush=True)

    def _proxy(self, base_url):
        split = urlsplit(self.path)
        target = f"{base_url}{split.path}"
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
                if key.lower() not in {"host", "content-length", "connection", "accept-encoding"}
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                self._send_upstream(response.status, response.headers, response.read())
        except urllib.error.HTTPError as response:
            self._send_upstream(response.code, response.headers, response.read())
        except Exception as exc:
            payload = str(exc).encode("utf-8", errors="replace")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

    def _send_upstream(self, status, headers, payload):
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() in {"transfer-encoding", "connection", "content-length"}:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _serve(self):
        if urlsplit(self.path).path.startswith("/api"):
            self._proxy(BACKEND_BASE)
            return
        self._proxy(FRONTEND_BASE)

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve()

    def do_POST(self):
        self._proxy(BACKEND_BASE)

    def do_PUT(self):
        self._proxy(BACKEND_BASE)

    def do_PATCH(self):
        self._proxy(BACKEND_BASE)

    def do_DELETE(self):
        self._proxy(BACKEND_BASE)


def main():
    files = json.load(sys.stdin)
    check.materialize(files)
    data_root = Path("/tmp/preview-data")
    data_root.mkdir(parents=True, exist_ok=True)
    database = data_root / "app.db"
    check.DATABASE = database
    os.environ["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    os.environ["DEBUG"] = "false"
    print("隔离预览：正在准备数据库与后端依赖", flush=True)
    backend_root = ROOT / "backend"
    check.ensure_backend_dependencies(backend_root)
    check.command(["alembic", "upgrade", "head"], backend_root)
    print("隔离预览：正在复用依赖并启动前端开发服务器", flush=True)
    check.ensure_frontend_dependencies()
    backend = check.start_backend(port=8000)
    vite_entry = ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
    frontend = subprocess.Popen(
        [
            "node",
            str(vite_entry),
            "--host",
            "127.0.0.1",
            "--port",
            "5173",
            "--strictPort",
        ],
        cwd=ROOT / "frontend",
    )
    check.wait_http(FRONTEND_BASE, frontend, "前端开发服务器")
    server = ThreadingHTTPServer(("0.0.0.0", 8080), PreviewHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("隔离预览已就绪：http://0.0.0.0:8080", flush=True)
    try:
        while backend.poll() is None and frontend.poll() is None and thread.is_alive():
            time.sleep(0.5)
        if backend.poll() is not None:
            raise RuntimeError(f"生成应用后端已退出，exit_code={backend.returncode}")
        if frontend.poll() is not None:
            raise RuntimeError(f"前端开发服务器已退出，exit_code={frontend.returncode}")
        raise RuntimeError("可信预览 HTTP 代理已退出")
    finally:
        server.shutdown()
        server.server_close()
        if frontend.poll() is None:
            check.stop_process(frontend)
        if backend.poll() is None:
            check.stop_process(backend)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"隔离预览启动失败：{exc}", file=sys.stderr, flush=True)
        raise
