"""Execute fixed engineering checks in disposable Linux containers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import threading
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.core.exceptions import BusinessException
from app.core.settings import settings
from app.schemas.agent_action import ToolExecutionResult
from app.tools.paths import MAX_FILE_BYTES, SKIP_DIRS, is_text_file, safe_path_under_root

CHECK_IDS = {"database", "backend", "frontend", "all"}
CHECK_RUNTIME_LABEL = "org.forgeai.check-runtime-version"
CHECK_RUNTIME_VERSION = "2"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_LOG_BYTES = 32 * 1024
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
VISUAL_VIEWPORTS = {
    "desktop": (1440, 900),
    "mobile": (390, 844),
}


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
            [
                binary,
                "image",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{CHECK_RUNTIME_LABEL}" }}}}',
                settings.engineering_check_image,
            ],
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
        runtime_version = image.stdout.strip()
        if runtime_version != CHECK_RUNTIME_VERSION:
            return result.model_copy(
                update={
                    "error_code": "CHECK_ENVIRONMENT_UNAVAILABLE",
                    "summary": (
                        "隔离检查镜像版本过旧：请重新构建 sandbox/Dockerfile，镜像名为 "
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
                "runtime_version": runtime_version,
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
    # Egress is required when generated package.json / pyproject.toml diverges from
    # the template baseline so the check controller can install the declared deps.
    # Source is still stdin-only; no host mounts or Docker socket.
    return [
        binary,
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65534:65534",
        "--cpus=2",
        "--memory=1536m",
        "--memory-swap=1536m",
        "--pids-limit=128",
        "--log-driver=none",
        "--init",
        "--tmpfs=/tmp:rw,exec,nosuid,nodev,size=768m,mode=1777",
        "-i",
        "--entrypoint=python",
        image,
        "-I",
        "/opt/forgeai/check.py",
        check_id,
    ]


def visual_docker_command(binary: str, image: str, name: str, route: str) -> list[str]:
    """Keep the stopped container long enough to copy binary screenshot evidence out."""

    command = docker_command(binary, image, name, "visual")
    command.remove("--rm")
    command.append(route)
    return command


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
        result.summary = (
            ("完整检查与隔离运行冒烟通过" if check_id == "all" else f"{check_id} 检查通过")
            if result.ok
            else f"{check_id} 检查失败"
        )
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


def _validated_route(route: str) -> str:
    value = route.strip() or "/"
    if (
        len(value) > 500
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise BusinessException("截图路由必须是站内绝对路径，例如 / 或 /jobs")
    return value


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise BusinessException("浏览器没有生成有效的 PNG 截图")
    return struct.unpack(">II", payload[16:24])


def _collect_visual_evidence(
    binary: str,
    container_name: str,
    root: Path,
    source_hash: str,
    visual_check_id: str,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with TemporaryDirectory(prefix="forgeai-visual-") as directory:
        copied = subprocess.run(
            [binary, "cp", f"{container_name}:/tmp/evidence/.", directory],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=flags,
            check=False,
        )
        if copied.returncode != 0:
            detail = (copied.stderr or copied.stdout).strip()
            raise BusinessException(f"无法读取浏览器截图证据：{detail[:300]}")
        temporary = Path(directory)
        manifest_path = temporary / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BusinessException("浏览器截图清单缺失或无效") from exc
        if not isinstance(manifest, dict):
            raise BusinessException("浏览器截图清单格式无效")
        raw_results = manifest.get("viewports")
        if not isinstance(raw_results, list):
            raise BusinessException("浏览器截图缺少视口结果")
        results_by_name = {
            str(item.get("name")): item for item in raw_results if isinstance(item, dict)
        }
        artifact_dir = root / "forgeai" / "evidence" / source_hash
        if visual_check_id != "visual":
            artifact_dir /= visual_check_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        screenshots: list[dict[str, object]] = []
        for name, expected_dimensions in VISUAL_VIEWPORTS.items():
            source = temporary / f"{name}.png"
            try:
                payload = source.read_bytes()
            except OSError as exc:
                raise BusinessException(f"缺少 {name} 视口截图") from exc
            if len(payload) > MAX_SCREENSHOT_BYTES:
                raise BusinessException(f"{name} 视口截图超过大小上限")
            dimensions = _png_dimensions(payload)
            if dimensions != expected_dimensions:
                raise BusinessException(
                    f"{name} 视口尺寸异常：期望 {expected_dimensions[0]}x{expected_dimensions[1]}，"
                    f"实际 {dimensions[0]}x{dimensions[1]}"
                )
            destination = artifact_dir / f"{name}.png"
            destination.write_bytes(payload)
            browser_result = results_by_name.get(name, {})
            screenshots.append(
                {
                    "viewport": name,
                    "width": dimensions[0],
                    "height": dimensions[1],
                    "path": destination.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                    "horizontal_overflow": bool(browser_result.get("horizontal_overflow")),
                    "console_errors": list(browser_result.get("console_errors") or [])[:20],
                    "page_errors": list(browser_result.get("page_errors") or [])[:20],
                }
            )
        return screenshots, manifest


def capture_screenshots(
    root: Path,
    *,
    route: str = "/",
    tool_call_id: str = "capture_screenshots",
) -> ToolExecutionResult:
    """Render the exact generated revision in Chromium at fixed desktop/mobile viewports."""

    result = ToolExecutionResult(
        tool_call_id=tool_call_id,
        name="capture_screenshots",
        ok=False,
        summary="",
    )
    try:
        route = _validated_route(route)
    except BusinessException as exc:
        return result.model_copy(update={"error_code": "INVALID_ROUTE", "summary": str(exc)})
    binary = shutil.which("docker")
    if binary is None:
        return result.model_copy(
            update={
                "error_code": "CHECK_ENVIRONMENT_UNAVAILABLE",
                "summary": "隔离截图环境未就绪：请启动 Docker 并重新构建 sandbox/Dockerfile。",
            }
        )
    try:
        payload, digest = source_snapshot(root)
    except (BusinessException, OSError, UnicodeError) as exc:
        return result.model_copy(update={"error_code": "INVALID_SOURCE", "summary": str(exc)})
    name = f"forgeai-visual-{uuid4().hex}"
    visual_check_id = (
        "visual"
        if route == "/"
        else f"visual_{hashlib.sha256(route.encode('utf-8')).hexdigest()[:12]}"
    )
    result.data = {"check_id": visual_check_id, "source_hash": digest, "route": route}
    try:
        code, output, truncated = _execute(
            visual_docker_command(binary, settings.engineering_check_image, name, route),
            payload,
            max(10, min(settings.engineering_check_timeout_seconds, 300)),
        )
        result.truncated = truncated
        result.data.update(exit_code=code, output=output)
        if code != 0:
            stale_runtime = "Unknown check" in output
            result.error_code = (
                "CHECK_ENVIRONMENT_UNAVAILABLE"
                if code in {125, 126, 127} or stale_runtime
                else "VISUAL_CHECK_FAILED"
            )
            result.summary = (
                "隔离截图镜像版本过旧，请重新构建 sandbox/Dockerfile 后继续"
                if stale_runtime
                else "浏览器截图失败"
            )
            return result
        screenshots, manifest = _collect_visual_evidence(
            binary, name, root, digest, visual_check_id
        )
        result.data.update(screenshots=screenshots, browser=manifest.get("browser"))
        has_runtime_error = any(
            item["horizontal_overflow"] or item["console_errors"] or item["page_errors"]
            for item in screenshots
        )
        result.ok = not has_runtime_error
        result.error_code = None if result.ok else "VISUAL_CHECK_FAILED"
        result.summary = (
            "桌面端与移动端浏览器截图及视口检查通过"
            if result.ok
            else "已生成桌面端与移动端截图，但检测到控制台、页面或横向溢出错误"
        )
        if source_snapshot(root)[1] != digest:
            result.ok = False
            result.error_code = "SOURCE_CHANGED"
            result.summary = "截图期间源码发生变化，需要重新截图"
    except subprocess.TimeoutExpired:
        result.error_code, result.summary = "CHECK_TIMEOUT", "浏览器截图超时"
    except BusinessException as exc:
        result.error_code, result.summary = "VISUAL_CHECK_FAILED", str(exc)[:500]
    except (OSError, UnicodeError) as exc:
        result.error_code, result.summary = "CHECK_ENVIRONMENT_UNAVAILABLE", str(exc)[:500]
    finally:
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
