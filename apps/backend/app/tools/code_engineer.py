"""Engineering tool registry. The model proposes calls; this module executes them."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.exceptions import BusinessException, ConflictException
from app.schemas.agent_action import ToolCall, ToolDefinition, ToolExecutionResult
from app.tools import checks as check_tools
from app.tools import files as file_tools

CODE_ENGINEER_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="list_files",
        description="列出当前生成应用工作区内的文本文件。path 为相对目录，默认根目录。",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="read_file",
        description="读取工作区文本文件的指定行范围，并返回 content_hash。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="search_code",
        description="在工作区中按字符串搜索代码，可选 path_filter 限制路径子串。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path_filter": {"type": "string"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="apply_patch",
        description="整文件写入。已有文件必须提供读取时的 expected_hash；冲突时不要覆盖。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "expected_hash": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="run_check",
        description="在离线隔离环境中检查数据库迁移、后端启动和前端构建；完成前使用 all。",
        parameters={
            "type": "object",
            "properties": {
                "check_id": {
                    "type": "string",
                    "enum": ["database", "backend", "frontend", "all"],
                }
            },
            "required": ["check_id"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="complete_work_item",
        description="当前工作单元的代码已写入工作区。不能把整个工程任务标为用户可用，也不能跳过未实现的需求。",
        parameters={
            "type": "object",
            "properties": {
                "work_item_id": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["work_item_id"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="report_blocked",
        description="获批需求无法在当前栈内实现，或缺少必要信息。不要伪造功能。",
        parameters={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    ),
]

ALLOWED_TOOL_NAMES = {tool.name for tool in CODE_ENGINEER_TOOLS}


def execute_tool_call(
    call: ToolCall,
    *,
    workspace_root: Path,
    allowed: set[str] | None = None,
    complete_work_item: Callable[[dict[str, Any]], ToolExecutionResult] | None = None,
    report_blocked: Callable[[dict[str, Any]], ToolExecutionResult] | None = None,
) -> ToolExecutionResult:
    allowed_names = allowed or ALLOWED_TOOL_NAMES
    if call.name not in allowed_names:
        return ToolExecutionResult(
            tool_call_id=call.id,
            name=call.name,
            ok=False,
            error_code="UNKNOWN_TOOL",
            summary=f"未知工具: {call.name}",
        )
    args = call.arguments
    try:
        if call.name == "list_files":
            return file_tools.list_files(
                workspace_root, path=str(args.get("path") or "."), tool_call_id=call.id
            )
        if call.name == "read_file":
            return file_tools.read_file(
                workspace_root,
                path=str(args.get("path") or ""),
                start_line=int(args.get("start_line") or 1),
                end_line=int(args["end_line"]) if args.get("end_line") is not None else None,
                tool_call_id=call.id,
            )
        if call.name == "search_code":
            return file_tools.search_code(
                workspace_root,
                query=str(args.get("query") or ""),
                path_filter=str(args["path_filter"]) if args.get("path_filter") else None,
                tool_call_id=call.id,
            )
        if call.name == "apply_patch":
            return file_tools.apply_patch(
                workspace_root,
                path=str(args.get("path") or ""),
                content=str(args.get("content") or ""),
                expected_hash=str(args["expected_hash"]) if args.get("expected_hash") else None,
                tool_call_id=call.id,
            )
        if call.name == "run_check":
            return check_tools.run_check(
                workspace_root,
                check_id=str(args.get("check_id") or ""),
                tool_call_id=call.id,
            )
        if call.name == "complete_work_item":
            if complete_work_item is None:
                raise BusinessException("当前阶段不能结束工作单元")
            return complete_work_item(args)
        if call.name == "report_blocked":
            if report_blocked is None:
                raise BusinessException("当前阶段不能报告阻断")
            return report_blocked(args)
    except (ConflictException, BusinessException) as exc:
        return ToolExecutionResult(
            tool_call_id=call.id,
            name=call.name,
            ok=False,
            error_code="TOOL_REJECTED",
            summary=str(exc),
        )
    return ToolExecutionResult(
        tool_call_id=call.id,
        name=call.name,
        ok=False,
        error_code="UNKNOWN_TOOL",
        summary=f"未实现工具: {call.name}",
    )
