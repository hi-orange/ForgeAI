"""Trusted dependency-manifest resolver baked into the sandbox image."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path("/tmp/app")
MAX_FILE_BYTES = 2 * 1024 * 1024
RESULT_MARKER = "FORGEAI_DEPENDENCY_RESULT="
PACKAGE_SPEC = re.compile(
    r"^(?:@[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._+-]*(?:@[^;\s|&<>`$()]+)?$"
)


def _load_check_controller():
    spec = importlib.util.spec_from_file_location("forgeai_trusted_check", "/opt/forgeai/check.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("隔离依赖控制器缺少 check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load_check_controller()


def _request():
    value = json.load(sys.stdin)
    if not isinstance(value, dict) or not isinstance(value.get("files"), dict):
        raise ValueError("依赖请求格式无效")
    manager = value.get("manager")
    action = value.get("action")
    packages = value.get("packages")
    if manager not in {"npm", "uv"} or action not in {"add", "remove", "sync"}:
        raise ValueError("依赖操作不受支持")
    if not isinstance(packages, list) or len(packages) > 8:
        raise ValueError("依赖列表无效")
    if action != "sync" and not packages:
        raise ValueError("依赖列表不能为空")
    if any(not isinstance(item, str) or not PACKAGE_SPEC.fullmatch(item) for item in packages):
        raise ValueError("依赖规格无效")
    if action == "sync" and (manager != "npm" or packages):
        raise ValueError("sync 只支持 npm 空依赖列表")
    return value["files"], manager, action, packages


def _argv(manager, action, packages):
    if manager == "npm":
        base = ["npm", "install" if action in {"add", "sync"} else "uninstall"]
        return [
            *base,
            "--package-lock-only",
            "--no-fund",
            "--no-audit",
            "--ignore-scripts",
            *packages,
        ]
    return ["uv", "add" if action == "add" else "remove", "--no-sync", *packages]


def _read(relative):
    path = ROOT / relative
    payload = path.read_bytes()
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError(f"依赖结果文件过大：{relative}")
    return payload.decode("utf-8")


def main():
    files, manager, action, packages = _request()
    check.materialize(files)
    target = ROOT / ("frontend" if manager == "npm" else "backend")
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": "/tmp",
        "CI": "1",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_AUDIT": "false",
        "UV_NO_PROGRESS": "1",
    }
    completed = subprocess.run(
        _argv(manager, action, packages),
        cwd=target,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=150,
        check=False,
    )
    print(completed.stdout[-12000:], flush=True)
    if completed.returncode != 0:
        raise RuntimeError(f"依赖解析失败，exit_code={completed.returncode}")
    paths = (
        ("frontend/package.json", "frontend/package-lock.json")
        if manager == "npm"
        else ("backend/pyproject.toml", "backend/uv.lock")
    )
    result = json.dumps(
        {"files": {path: _read(path) for path in paths}},
        ensure_ascii=False,
    ).encode("utf-8")
    print(RESULT_MARKER + base64.b64encode(result).decode("ascii"), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"隔离依赖解析失败：{exc}", file=sys.stderr, flush=True)
        raise
