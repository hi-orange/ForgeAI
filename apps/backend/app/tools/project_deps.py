"""Resolve generated-project dependency manifests inside a disposable container."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from app.core.exceptions import BusinessException
from app.core.settings import settings
from app.runtime.docker import get_docker_sandbox_runtime
from app.schemas.agent_action import ToolExecutionResult
from app.tools.paths import safe_path_under_root

Manager = Literal["npm", "uv"]
Action = Literal["add", "remove"]
Target = Literal["frontend", "backend"]

MAX_PACKAGES = 8
MAX_OUTPUT_CHARS = 12_000
MAX_RESULT_BYTES = 2 * 1024 * 1024
INSTALL_TIMEOUT_SECONDS = 180
DEPENDENCY_RUNTIME_LABEL = "org.forgeai.dependency-runtime-version"
DEPENDENCY_RUNTIME_VERSION = "2"
_RESULT_MARKER = b"FORGEAI_DEPENDENCY_RESULT="

_PACKAGE_SPEC = re.compile(
    r"^(?:@[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._+-]*(?:@[^;\s|&<>`$()]+)?$"
)
_BLOCKED_SUBSTRINGS = (
    "..",
    ";",
    "|",
    "&",
    "`",
    "$",
    "\n",
    "\r",
    ">",
    "<",
    "$(",
    "curl",
    "wget",
    "apt",
    "yum",
    "brew",
    "sudo",
    "powershell",
    "cmd.exe",
)
_RESULT_PATHS: dict[Manager, tuple[str, ...]] = {
    "npm": ("frontend/package.json", "frontend/package-lock.json"),
    "uv": ("backend/pyproject.toml", "backend/uv.lock"),
}


def _normalize_packages(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise BusinessException("packages 必须是非空数组")
    if len(raw) > MAX_PACKAGES:
        raise BusinessException(f"单次最多安装或移除 {MAX_PACKAGES} 个包")
    packages: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise BusinessException("packages 每项必须是字符串")
        spec = item.strip()
        if not spec or not _PACKAGE_SPEC.fullmatch(spec):
            raise BusinessException(f"非法包名或版本规格：{item!r}")
        lowered = spec.lower()
        if any(token in lowered for token in _BLOCKED_SUBSTRINGS):
            raise BusinessException(f"拒绝不安全的依赖规格：{item!r}")
        packages.append(spec)
    return packages


def _validate_pairing(manager: str, target: str) -> tuple[Manager, Target]:
    if manager not in {"npm", "uv"}:
        raise BusinessException("当前模板只支持 frontend/npm 与 backend/uv")
    if target not in {"frontend", "backend"}:
        raise BusinessException("target 只允许 frontend 或 backend")
    if (manager, target) not in {("npm", "frontend"), ("uv", "backend")}:
        raise BusinessException("当前模板只支持 frontend/npm 与 backend/uv")
    return manager, target  # type: ignore[return-value]


def build_install_argv(manager: Manager, action: Action, packages: list[str]) -> list[str]:
    if action not in {"add", "remove"}:
        raise BusinessException("action 只允许 add 或 remove")
    if manager == "npm":
        if action == "add":
            return [
                "npm",
                "install",
                "--package-lock-only",
                "--no-fund",
                "--no-audit",
                "--ignore-scripts",
                *packages,
            ]
        return [
            "npm",
            "uninstall",
            "--package-lock-only",
            "--no-fund",
            "--no-audit",
            "--ignore-scripts",
            *packages,
        ]
    if action == "add":
        return ["uv", "add", "--no-sync", *packages]
    return ["uv", "remove", "--no-sync", *packages]


def dependency_docker_command(binary: str, image: str, name: str) -> list[str]:
    """Return the fixed no-mount dependency resolver invocation."""

    return [
        binary,
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--hostname=dependency-resolver",
        *get_docker_sandbox_runtime().isolation_args(
            cpus=1.5,
            memory_mb=1024,
            pids_limit=192,
            tmpfs="rw,noexec,nosuid,nodev,size=768m,mode=1777",
        ),
        "-i",
        "--entrypoint=python",
        image,
        "-I",
        "/opt/forgeai/dependencies.py",
    ]


def _runtime() -> tuple[str, str]:
    image = settings.dependency_runtime_image
    binary = get_docker_sandbox_runtime().require_image(
        image,
        label=DEPENDENCY_RUNTIME_LABEL,
        version=DEPENDENCY_RUNTIME_VERSION,
        purpose="隔离依赖",
    )
    return binary, image


def _manifest_snapshot(workspace_root: Path, manager: Manager) -> tuple[dict[str, str], str]:
    """Read only dependency inputs, so the networked resolver never receives application source."""

    files: dict[str, str] = {}
    digest = hashlib.sha256()
    for index, relative in enumerate(_RESULT_PATHS[manager]):
        path = safe_path_under_root(workspace_root, relative, allow_create=True)
        if not path.is_file():
            if index == 0:
                raise BusinessException(f"工作区缺少 {relative}")
            continue
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_RESULT_BYTES:
                raise BusinessException(f"依赖清单超过大小上限：{relative}")
            content = raw.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise BusinessException(f"无法读取依赖清单：{relative}") from exc
        files[relative] = content
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return files, digest.hexdigest()


def _remove_container(binary: str, name: str) -> None:
    get_docker_sandbox_runtime().remove_container(binary, name)


def _read_result(output: bytes, manager: Manager) -> dict[str, bytes]:
    encoded = next(
        (
            line.removeprefix(_RESULT_MARKER)
            for line in reversed(output.splitlines())
            if line.startswith(_RESULT_MARKER)
        ),
        None,
    )
    if encoded is None:
        raise BusinessException("隔离依赖结果缺失")
    if len(encoded) > (MAX_RESULT_BYTES * 2):
        raise BusinessException("隔离依赖结果超过大小上限")
    try:
        decoded = base64.b64decode(encoded, validate=True)
        if len(decoded) > MAX_RESULT_BYTES:
            raise BusinessException("隔离依赖结果超过大小上限")
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeError, json.JSONDecodeError) as exc:
        raise BusinessException("隔离依赖结果无效") from exc
    raw_files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(raw_files, dict) or set(raw_files) != set(_RESULT_PATHS[manager]):
        raise BusinessException("隔离依赖结果包含未授权文件")
    updates: dict[str, bytes] = {}
    for relative, content in raw_files.items():
        if not isinstance(content, str):
            raise BusinessException("隔离依赖结果文件格式无效")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_RESULT_BYTES:
            raise BusinessException("隔离依赖结果文件超过大小上限")
        updates[relative] = encoded
    return updates


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.forgeai-{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _apply_updates(root: Path, updates: dict[str, bytes]) -> None:
    paths = {
        relative: safe_path_under_root(root, relative, allow_create=True) for relative in updates
    }
    previous = {
        relative: path.read_bytes() if path.is_file() else None for relative, path in paths.items()
    }
    applied: list[str] = []
    try:
        for relative, path in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, updates[relative])
            applied.append(relative)
    except OSError as exc:
        for relative in reversed(applied):
            old = previous[relative]
            path = paths[relative]
            if old is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, old)
        raise BusinessException("依赖清单写回失败，已恢复原文件") from exc


def _run_isolated(
    *,
    workspace_root: Path,
    manager: Manager,
    action: Action | Literal["sync"],
    packages: list[str],
) -> tuple[dict[str, bytes], str, bool]:
    binary, image = _runtime()
    files, digest = _manifest_snapshot(workspace_root, manager)
    request = json.dumps(
        {"files": files, "manager": manager, "action": action, "packages": packages},
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    name = f"forgeai-dependency-{uuid4().hex}"
    try:
        completed = get_docker_sandbox_runtime().run_bytes(
            dependency_docker_command(binary, image, name),
            payload=request,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
        output_bytes = completed.stdout + completed.stderr
        visible_output = b"\n".join(
            line for line in output_bytes.splitlines() if not line.startswith(_RESULT_MARKER)
        )
        output = visible_output.decode("utf-8", errors="replace")[-MAX_OUTPUT_CHARS:]
        if completed.returncode != 0:
            raise BusinessException(
                f"隔离依赖命令失败（退出码 {completed.returncode}）"
                + (f"：{output}" if output else "")
            )
        updates = _read_result(output_bytes, manager)
        if _manifest_snapshot(workspace_root, manager)[1] != digest:
            raise BusinessException("依赖解析期间清单发生变化，请重新执行")
        return updates, output, len(visible_output) > MAX_OUTPUT_CHARS
    except subprocess.TimeoutExpired as exc:
        raise BusinessException(f"依赖解析超时（>{INSTALL_TIMEOUT_SECONDS}s）") from exc
    except OSError as exc:
        raise BusinessException(f"隔离依赖命令无法启动：{exc}") from exc
    finally:
        _remove_container(binary, name)


def _execute(
    *,
    workspace_root: Path,
    manager: Manager,
    action: Action | Literal["sync"],
    packages: list[str],
    tool_call_id: str,
    arguments: dict[str, Any],
    success_summary: str,
) -> ToolExecutionResult:
    result = ToolExecutionResult(
        tool_call_id=tool_call_id,
        name="install_project_dependency",
        ok=False,
        summary="",
        arguments=arguments,
    )
    try:
        target = "frontend" if manager == "npm" else "backend"
        manifest = _RESULT_PATHS[manager][0]
        if not safe_path_under_root(workspace_root, manifest).parent.is_dir():
            raise BusinessException(f"工作区缺少 {target}/ 目录")
        updates, output, truncated = _run_isolated(
            workspace_root=workspace_root,
            manager=manager,
            action=action,
            packages=packages,
        )
        _apply_updates(workspace_root, updates)
        return result.model_copy(
            update={
                "ok": True,
                "summary": success_summary,
                "data": {
                    "runtime": "docker",
                    "updated_files": sorted(updates),
                    "output": output,
                },
                "truncated": truncated,
            }
        )
    except BusinessException as exc:
        return result.model_copy(
            update={"error_code": "DEPENDENCY_RESOLUTION_FAILED", "summary": str(exc)}
        )


def install_project_dependency(
    workspace_root: Path,
    *,
    manager: str,
    action: str,
    packages: Any,
    target: str = "frontend",
    tool_call_id: str = "install_project_dependency",
) -> ToolExecutionResult:
    """Update dependency manifests in Docker; never execute generated packages on the host."""

    arguments = {"manager": manager, "action": action, "packages": packages, "target": target}
    try:
        chosen_manager, chosen_target = _validate_pairing(str(manager), str(target))
        if action not in {"add", "remove"}:
            raise BusinessException("action 只允许 add 或 remove")
        package_list = _normalize_packages(packages)
        return _execute(
            workspace_root=workspace_root,
            manager=chosen_manager,
            action=action,  # type: ignore[arg-type]
            packages=package_list,
            tool_call_id=tool_call_id,
            arguments=arguments,
            success_summary=(
                f"已在隔离环境更新 {chosen_target}/：{action} {', '.join(package_list)}"
            ),
        )
    except BusinessException as exc:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="install_project_dependency",
            ok=False,
            error_code="TOOL_REJECTED",
            summary=str(exc),
            arguments=arguments,
        )


def sync_frontend_package_lock(
    workspace_root: Path,
    *,
    tool_call_id: str = "sync_frontend_package_lock",
) -> ToolExecutionResult:
    """Resolve package-lock.json from package.json inside the dependency sandbox."""

    return _execute(
        workspace_root=workspace_root,
        manager="npm",
        action="sync",
        packages=[],
        tool_call_id=tool_call_id,
        arguments={"manager": "npm", "action": "sync", "packages": [], "target": "frontend"},
        success_summary="已在隔离环境按 frontend/package.json 同步锁文件",
    )


def sync_manifest_dependencies(
    workspace_root: Path,
    relative_path: str,
    *,
    tool_call_id: str = "sync_manifest_dependencies",
) -> ToolExecutionResult | None:
    """After a frontend manifest write, reconcile its lock file in isolation."""

    normalized = relative_path.replace("\\", "/").strip()
    if normalized == "frontend/package.json":
        return sync_frontend_package_lock(workspace_root, tool_call_id=tool_call_id)
    return None
