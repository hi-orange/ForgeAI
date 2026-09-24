"""Bounded project-dependency installs for generated workspaces.

Runtime baseline (Node/Python toolchains) stays platform-owned. Agents may only
add/remove packages inside frontend/ or backend/ via allowlisted package managers.
System packages (apt, brew, etc.) are rejected.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

from app.core.exceptions import BusinessException
from app.schemas.agent_action import ToolExecutionResult
from app.tools.paths import safe_path_under_root

Manager = Literal["npm", "pnpm", "uv", "pip"]
Action = Literal["add", "remove"]
Target = Literal["frontend", "backend"]

MAX_PACKAGES = 8
MAX_OUTPUT_CHARS = 12_000
INSTALL_TIMEOUT_SECONDS = 180

# Scoped npm names (@scope/pkg) and optional version / npm tag suffix.
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
    if manager not in {"npm", "pnpm", "uv", "pip"}:
        raise BusinessException("只支持 npm、pnpm、uv、pip")
    if target not in {"frontend", "backend"}:
        raise BusinessException("target 只允许 frontend 或 backend")
    if manager in {"npm", "pnpm"} and target != "frontend":
        raise BusinessException("npm/pnpm 只能用于 frontend")
    if manager in {"uv", "pip"} and target != "backend":
        raise BusinessException("uv/pip 只能用于 backend")
    return manager, target  # type: ignore[return-value]


def build_install_argv(manager: Manager, action: Action, packages: list[str]) -> list[str]:
    if action not in {"add", "remove"}:
        raise BusinessException("action 只允许 add 或 remove")
    if manager == "npm":
        if action == "add":
            return ["npm", "install", "--no-fund", "--no-audit", "--ignore-scripts", *packages]
        return ["npm", "uninstall", "--no-fund", "--no-audit", *packages]
    if manager == "pnpm":
        if action == "add":
            return ["pnpm", "add", "--ignore-scripts", *packages]
        return ["pnpm", "remove", *packages]
    if manager == "uv":
        if action == "add":
            return ["uv", "add", *packages]
        return ["uv", "remove", *packages]
    if action == "remove":
        raise BusinessException("pip 不支持 remove；请编辑 backend/pyproject.toml 后重新检查")
    return ["pip", "install", "--no-cache-dir", *packages]


def _run_argv(
    *,
    workspace_root: Path,
    target: Target,
    argv: list[str],
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
        cwd = safe_path_under_root(workspace_root, target, allow_create=True)
        if not cwd.is_dir():
            raise BusinessException(f"工作区缺少 {target}/ 目录")
        binary = shutil.which(argv[0])
        if binary is None:
            raise BusinessException(
                f"本机未找到 {argv[0]}；请安装后重试，或仅更新清单并依赖隔离检查安装"
            )
        resolved = [binary, *argv[1:]]
        completed = subprocess.run(
            resolved,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=INSTALL_TIMEOUT_SECONDS,
            check=False,
            shell=False,
            env={**os.environ, "CI": "1", "NPM_CONFIG_FUND": "false", "NPM_CONFIG_AUDIT": "false"},
        )
        output = (completed.stdout + completed.stderr)[:MAX_OUTPUT_CHARS]
        ok = completed.returncode == 0
        return result.model_copy(
            update={
                "ok": ok,
                "error_code": None if ok else "DEPENDENCY_INSTALL_FAILED",
                "summary": (
                    success_summary
                    if ok
                    else f"{target}/ 依赖命令失败（退出码 {completed.returncode}）"
                ),
                "data": {
                    "argv": resolved,
                    "cwd": target,
                    "exit_code": completed.returncode,
                    "output": output,
                },
                "truncated": len(completed.stdout) + len(completed.stderr) > MAX_OUTPUT_CHARS,
            }
        )
    except BusinessException as exc:
        return result.model_copy(update={"error_code": "TOOL_REJECTED", "summary": str(exc)})
    except subprocess.TimeoutExpired:
        return result.model_copy(
            update={
                "error_code": "DEPENDENCY_INSTALL_TIMEOUT",
                "summary": f"依赖安装超时（>{INSTALL_TIMEOUT_SECONDS}s）",
            }
        )
    except OSError as exc:
        return result.model_copy(
            update={"error_code": "TOOL_REJECTED", "summary": f"依赖安装无法启动：{exc}"}
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
    """Run an allowlisted package-manager command inside frontend/ or backend/."""

    arguments = {
        "manager": manager,
        "action": action,
        "packages": packages,
        "target": target,
    }
    try:
        chosen_manager, chosen_target = _validate_pairing(str(manager), str(target))
        if action not in {"add", "remove"}:
            raise BusinessException("action 只允许 add 或 remove")
        package_list = _normalize_packages(packages)
        argv = build_install_argv(chosen_manager, action, package_list)  # type: ignore[arg-type]
        return _run_argv(
            workspace_root=workspace_root,
            target=chosen_target,
            argv=argv,
            tool_call_id=tool_call_id,
            arguments=arguments,
            success_summary=f"已在 {chosen_target}/ {action} {', '.join(package_list)}",
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
    """Install from the current frontend/package.json without adding named packages."""

    return _run_argv(
        workspace_root=workspace_root,
        target="frontend",
        argv=["npm", "install", "--no-fund", "--no-audit", "--ignore-scripts"],
        tool_call_id=tool_call_id,
        arguments={"manager": "npm", "action": "sync", "packages": [], "target": "frontend"},
        success_summary="已按 frontend/package.json 同步依赖",
    )


def sync_manifest_dependencies(
    workspace_root: Path,
    relative_path: str,
    *,
    tool_call_id: str = "sync_manifest_dependencies",
) -> ToolExecutionResult | None:
    """After writing a dependency manifest, sync the matching project tree when possible."""

    normalized = relative_path.replace("\\", "/").strip()
    if normalized == "frontend/package.json":
        return sync_frontend_package_lock(workspace_root, tool_call_id=tool_call_id)
    return None
