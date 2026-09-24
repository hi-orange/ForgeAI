"""Execute fixed engineering checks in disposable, offline Linux containers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path
from uuid import uuid4

from app.core.exceptions import BusinessException
from app.core.settings import settings
from app.schemas.agent_action import ToolExecutionResult
from app.tools.paths import MAX_FILE_BYTES, SKIP_DIRS, is_text_file, safe_path_under_root

CHECK_IDS = {"database", "backend", "frontend", "all"}
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_LOG_BYTES = 32 * 1024


def check_environment(*, tool_call_id: str = "check_environment") -> ToolExecutionResult:
    """Verify the isolated check runtime before spending model calls."""

    result = ToolExecutionResult(
        tool_call_id=tool_call_id,
        name="check_environment",
        ok=False,
        summary="",
    )
    binary = shutil.which("docker")
    if binary is None:
        return result.model_copy(
            update={
                "error_code": "CHECK_ENVIRONMENT_UNAVAILABLE",
                "summary": "隔离检查环境未就绪：未找到 Docker，请安装并启动 Docker。",
            }
        )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        daemon = subprocess.run(
            [binary, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=flags,
            check=False,
        )
        if daemon.returncode != 0:
            detail = (daemon.stderr or daemon.stdout).strip()
            return result.model_copy(
                update={
                    "error_code": "CHECK_ENVIRONMENT_UNAVAILABLE",
                    "summary": (
                        "隔离检查环境未就绪：Docker daemon 不可用，请启动 Docker 后继续。"
                        + (f" {detail[:240]}" if detail else "")
                    ),
                }
            )
        image = subprocess.run(
            [binary, "image", "inspect", settings.engineering_check_image],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=flags,
            check=False,
        )
        if image.returncode != 0:
            return result.model_copy(
                update={
                    "error_code": "CHECK_ENVIRONMENT_UNAVAILABLE",
                    "summary": (
                        "隔离检查镜像不存在：请先构建 sandbox/Dockerfile，镜像名为 "
                        f"{settings.engineering_check_image}。"
                    ),
                }
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return result.model_copy(
            update={
                "error_code": "CHECK_ENVIRONMENT_UNAVAILABLE",
                "summary": f"隔离检查环境探测失败：{str(exc)[:240]}",
            }
        )
    return result.model_copy(
        update={
            "ok": True,
            "summary": "隔离检查环境已就绪",
            "data": {
                "image": settings.engineering_check_image,
                "server_version": daemon.stdout.strip(),
            },
        }
    )


def source_snapshot(root: Path) -> tuple[bytes, str]:
    """Export only app sources, never platform markers, secrets, data or symlinks."""
    files: dict[str, str] = {}
    size = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts) or not path.is_file():
            continue
        if relative.parts[0] == "forgeai":
            continue
        if not is_text_file(path) or path.name.startswith(".env"):
            continue
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root):
            raise BusinessException("工作区包含符号链接，不能交给执行器")
        safe_path_under_root(root, relative.as_posix())
        raw = path.read_bytes()
        size += len(raw)
        if len(raw) > MAX_FILE_BYTES or size > MAX_SOURCE_BYTES or len(files) >= 400:
            raise BusinessException("工作区源码超过检查上限")
        files[relative.as_posix()] = raw.decode("utf-8")
    payload = json.dumps(files, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return payload, hashlib.sha256(payload).hexdigest()


def docker_command(binary: str, image: str, name: str, check_id: str) -> list[str]:
    return [
        binary,
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65534:65534",
        "--cpus=2",
        "--memory=768m",
        "--memory-swap=768m",
        "--pids-limit=96",
        "--log-driver=none",
        "--init",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=384m,mode=1777",
        "-i",
        "--entrypoint=python",
        image,
        "-I",
        "/opt/forgeai/check.py",
        check_id,
    ]


def _execute(command: list[str], payload: bytes, timeout: int) -> tuple[int, str, bool]:
    """Drain output continuously but retain only a bounded tail, even for noisy code."""
    chunks: deque[bytes] = deque(maxlen=MAX_LOG_BYTES // 1024)
    count = 0
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )

    def read() -> None:
        nonlocal count
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read(1024):
                chunks.append(chunk)
                count += len(chunk)
        finally:
            process.stdout.close()

    def write() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(payload)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    reader = threading.Thread(target=read, daemon=True)
    writer = threading.Thread(target=write, daemon=True)
    reader.start()
    writer.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
        raise
    finally:
        reader.join(timeout=5)
        writer.join(timeout=5)
    return code, b"".join(chunks).decode("utf-8", errors="replace"), count > MAX_LOG_BYTES


def run_check(root: Path, *, check_id: str, tool_call_id: str = "check") -> ToolExecutionResult:
    result = ToolExecutionResult(tool_call_id=tool_call_id, name="run_check", ok=False, summary="")
    if check_id not in CHECK_IDS:
        return result.model_copy(update={"error_code": "UNKNOWN_CHECK", "summary": "未知检查"})
    binary = shutil.which("docker")
    if binary is None:
        return result.model_copy(
            update={
                "error_code": "CHECK_ENVIRONMENT_UNAVAILABLE",
                "summary": "隔离环境未就绪：请启动 Docker 并构建 sandbox/Dockerfile 后继续。",
            }
        )
    try:
        payload, digest = source_snapshot(root)
    except (BusinessException, OSError, UnicodeError) as exc:
        return result.model_copy(update={"error_code": "INVALID_SOURCE", "summary": str(exc)})
    name = f"forgeai-check-{uuid4().hex}"
    result.data = {"check_id": check_id, "source_hash": digest}
    try:
        code, output, truncated = _execute(
            docker_command(binary, settings.engineering_check_image, name, check_id),
            payload,
            max(10, min(settings.engineering_check_timeout_seconds, 300)),
        )
        result.ok = code == 0
        result.error_code = (
            None
            if result.ok
            else ("CHECK_ENVIRONMENT_UNAVAILABLE" if code in {125, 126, 127} else "CHECK_FAILED")
        )
        result.summary = f"{check_id} 检查通过" if result.ok else f"{check_id} 检查失败"
        result.data.update(exit_code=code, output=output)
        result.truncated = truncated
        if source_snapshot(root)[1] != digest:
            result.ok, result.error_code, result.summary = (
                False,
                "SOURCE_CHANGED",
                "检查期间源码发生变化，需要重新检查",
            )
    except subprocess.TimeoutExpired:
        result.error_code, result.summary = "CHECK_TIMEOUT", "检查超时，已请求清理隔离实例"
    except (OSError, BusinessException, UnicodeError) as exc:
        result.error_code, result.summary = "CHECK_ENVIRONMENT_UNAVAILABLE", str(exc)[:500]
    finally:
        # Killing the CLI alone does not stop its container. Never address another run's instance.
        try:
            subprocess.run(
                [binary, "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result
