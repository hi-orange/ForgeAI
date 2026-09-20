"""Bounded editor and read-only terminal tools for Architect."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.exceptions import BusinessException, ConflictException
from app.schemas.agent_action import ToolCall, ToolDefinition, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.tools import files as file_tools
from app.tools.paths import SKIP_DIRS

MAX_TERMINAL_OUTPUT_CHARS = 12_000
MAX_ARCHITECT_INSPECTIONS = 4
_SYSTEM_DESIGN_SCHEMA = SystemDesign.model_json_schema()

ARCHITECT_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="read_artifact",
        description="读取本次任务冻结的已批准 PRD；不查询或替换为更新版本。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolDefinition(
        name="editor_read",
        description="读取当前系统设计草稿，或读取工作区中的一个必要文本文件。",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "minLength": 1, "maxLength": 500},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="editor_write",
        description="在编辑器中保存一版完整系统设计草稿，不提交正式成果。",
        parameters={
            "type": "object",
            "properties": {"system_design": _SYSTEM_DESIGN_SCHEMA},
            "required": ["system_design"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="terminal_list",
        description="在受限工作区内查看目录和文本文件清单。",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "maxLength": 500}},
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="terminal_run",
        description=(
            "在受限工作区运行只读命令。仅允许 rg --files、rg <文本>，"
            "以及 python/node/uv/pnpm --version。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 300},
                    "minItems": 2,
                    "maxItems": 3,
                }
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="write_system_design",
        description="提交最终完整 system_design，作为 Architect 的正式成果。",
        parameters={
            "type": "object",
            "properties": {"system_design": _SYSTEM_DESIGN_SCHEMA},
            "required": ["system_design"],
            "additionalProperties": False,
        },
    ),
]


@dataclass(slots=True)
class ArchitectToolState:
    app_spec: AppSpec
    workspace_root: Path
    draft: SystemDesign | None = None
    inspection_count: int = 0
    inspection_keys: set[str] = field(default_factory=set)


def _reserve_inspection(state: ArchitectToolState, key: str) -> None:
    if key in state.inspection_keys:
        raise BusinessException("该上下文已经读取过，请使用已有结果继续设计")
    if state.inspection_count >= MAX_ARCHITECT_INSPECTIONS:
        raise BusinessException("Architect 上下文读取预算已用尽，请提交系统设计")
    state.inspection_keys.add(key)
    state.inspection_count += 1


def _design_from_call(call: ToolCall) -> SystemDesign:
    try:
        return SystemDesign.model_validate(call.arguments.get("system_design"))
    except ValidationError as exc:
        raise BusinessException("系统设计缺少架构、模块、接口、数据结构或技术选型") from exc


def _validated_terminal_argv(root: Path, value: Any) -> list[str]:
    if not isinstance(value, list) or not 2 <= len(value) <= 3:
        raise BusinessException("终端命令参数不符合要求")
    argv = [str(part).strip() for part in value]
    if any(not part or "\x00" in part for part in argv):
        raise BusinessException("终端命令参数不符合要求")
    executable, arguments = argv[0], argv[1:]
    if executable in {"python", "node", "uv", "pnpm"}:
        if arguments != ["--version"]:
            raise BusinessException("Architect 终端只允许查询工具版本")
        return argv
    if executable != "rg":
        raise BusinessException("Architect 终端命令不在只读白名单中")
    if arguments == ["--files"]:
        return argv
    if len(arguments) > 2 or any(argument.startswith("-") for argument in arguments):
        raise BusinessException("Architect 的 rg 命令只允许文本搜索")
    if len(arguments) == 2:
        relative = arguments[1].replace("\\", "/").strip()
        if not relative or relative.startswith("/") or ".." in relative.split("/"):
            raise BusinessException("Architect 终端搜索路径不合法")
        if any(part in SKIP_DIRS for part in relative.split("/")):
            raise BusinessException("Architect 终端不能搜索该目录")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise BusinessException("Architect 终端搜索路径不合法") from exc
        if not candidate.exists():
            raise BusinessException("Architect 终端搜索路径不存在")
    return argv


def _run_terminal(root: Path, argv: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            cwd=root.resolve(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise BusinessException(f"终端工具不可用：{argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BusinessException("Architect 终端命令执行超时") from exc
    output = (completed.stdout + completed.stderr)[:MAX_TERMINAL_OUTPUT_CHARS]
    return {"argv": argv, "exit_code": completed.returncode, "output": output}


def execute_architect_tool(
    call: ToolCall,
    state: ArchitectToolState,
) -> tuple[ToolExecutionResult, SystemDesign | None]:
    try:
        if call.name == "read_artifact":
            _reserve_inspection(state, "artifact:app_spec")
            result = ToolExecutionResult(
                tool_call_id=call.id,
                name=call.name,
                ok=True,
                summary="已读取冻结 PRD",
                data={"app_spec": state.app_spec.model_dump(mode="json")},
                arguments=dict(call.arguments),
            )
            return result, None
        if call.name == "editor_read":
            target = str(call.arguments.get("target") or "").strip()
            if target == "draft":
                result = ToolExecutionResult(
                    tool_call_id=call.id,
                    name=call.name,
                    ok=state.draft is not None,
                    error_code=None if state.draft is not None else "DRAFT_NOT_FOUND",
                    summary="已读取系统设计草稿" if state.draft else "系统设计草稿尚未创建",
                    data={
                        "system_design": (
                            state.draft.model_dump(mode="json") if state.draft else None
                        )
                    },
                    arguments=dict(call.arguments),
                )
                return result, None
            _reserve_inspection(
                state,
                "editor:"
                f"{target}:{call.arguments.get('start_line') or 1}:"
                f"{call.arguments.get('end_line') or ''}",
            )
            result = file_tools.read_file(
                state.workspace_root,
                path=target,
                start_line=int(call.arguments.get("start_line") or 1),
                end_line=(
                    int(call.arguments["end_line"])
                    if call.arguments.get("end_line") is not None
                    else None
                ),
                tool_call_id=call.id,
            )
            return result.model_copy(
                update={"name": call.name, "arguments": dict(call.arguments)}
            ), None
        if call.name in {"editor_write", "write_system_design"}:
            design = _design_from_call(call)
            state.draft = design
            result = ToolExecutionResult(
                tool_call_id=call.id,
                name=call.name,
                ok=True,
                summary=(
                    "已保存系统设计草稿" if call.name == "editor_write" else "已提交最终系统设计"
                ),
                data={
                    "module_count": len(design.modules),
                    "interface_count": len(design.interfaces),
                    "data_structure_count": len(design.data_structures),
                },
                arguments=dict(call.arguments),
            )
            return result, design if call.name == "write_system_design" else None
        if call.name == "terminal_list":
            path = str(call.arguments.get("path") or ".")
            _reserve_inspection(state, f"list:{path}")
            result = file_tools.list_files(
                state.workspace_root,
                path=path,
                tool_call_id=call.id,
            )
            return result.model_copy(
                update={"name": call.name, "arguments": dict(call.arguments)}
            ), None
        if call.name == "terminal_run":
            argv = _validated_terminal_argv(state.workspace_root, call.arguments.get("argv"))
            _reserve_inspection(state, f"terminal:{argv!r}")
            data = _run_terminal(state.workspace_root, argv)
            result = ToolExecutionResult(
                tool_call_id=call.id,
                name=call.name,
                ok=data["exit_code"] in (0, 1),
                error_code=None if data["exit_code"] in (0, 1) else "COMMAND_FAILED",
                summary=f"只读终端命令退出码：{data['exit_code']}",
                data=data,
                arguments=dict(call.arguments),
                truncated=len(data["output"]) >= MAX_TERMINAL_OUTPUT_CHARS,
            )
            return result, None
        raise BusinessException(f"Architect 无权使用工具：{call.name}")
    except (BusinessException, ConflictException, OSError, ValueError) as exc:
        return (
            ToolExecutionResult(
                tool_call_id=call.id,
                name=call.name,
                ok=False,
                error_code="TOOL_REJECTED",
                summary=str(exc),
                arguments=dict(call.arguments),
            ),
            None,
        )
