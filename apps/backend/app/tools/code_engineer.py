"""Engineering tool registry. The model proposes calls; this module executes them."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.exceptions import BusinessException, ConflictException
from app.schemas.agent_action import ToolCall, ToolDefinition, ToolExecutionResult
from app.tools import checks as check_tools
from app.tools import files as file_tools
from app.tools import project_deps

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
        name="retrieve_code_context",
        description=(
            "RAG 兜底检索：仅当不知道代码位置且已做过精确 search_code 后，"
            "从当前工作区召回带路径、行范围和 hash 的候选片段；命中后仍需 read_file。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2, "maxLength": 500},
                "path_filter": {"type": "string", "maxLength": 500},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="edit_file_by_replace",
        description=(
            "局部修改已有文件：old_text 必须在读取版本中精确且唯一匹配；"
            "必须提供读取时的 expected_hash，冲突或多处命中时重新读取。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "minLength": 1},
                "new_text": {"type": "string"},
                "expected_hash": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text", "expected_hash"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="write_new_code",
        description=(
            "创建新文件或在明确需要时整体重写一个文件。平台会在事务外调用专门写码模型；"
            "已有文件必须提供读取时的 expected_hash。局部修改优先使用 edit_file_by_replace。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "file_description": {"type": "string", "minLength": 1, "maxLength": 1000},
                "expected_hash": {"type": "string"},
            },
            "required": ["path", "file_description"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="record_engineering_memory",
        description="记录一条有已读取或已写入文件证据的长期工程事实，供后续工作单元和恢复执行使用。",
        parameters={
            "type": "object",
            "properties": {
                "subject": {"type": "string", "minLength": 1, "maxLength": 120},
                "fact": {"type": "string", "minLength": 1, "maxLength": 600},
                "evidence_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 8,
                },
            },
            "required": ["subject", "fact", "evidence_paths"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="install_project_dependency",
        description=(
            "在 frontend/ 或 backend/ 内用白名单包管理器增删项目依赖"
            "（npm/pnpm/uv/pip）。禁止 apt 等系统安装；Runtime 基线由平台提供。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "manager": {
                    "type": "string",
                    "enum": ["npm", "pnpm", "uv", "pip"],
                },
                "action": {"type": "string", "enum": ["add", "remove"]},
                "packages": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 120},
                    "minItems": 1,
                    "maxItems": 8,
                },
                "target": {
                    "type": "string",
                    "enum": ["frontend", "backend"],
                    "description": "npm/pnpm→frontend；uv/pip→backend",
                },
            },
            "required": ["manager", "action", "packages", "target"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="run_check",
        description=(
            "在隔离环境中检查数据库迁移、后端启动和前端构建"
            "（可按生成清单安装依赖）；完成前使用 all。"
        ),
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
    write_new_code: Callable[[dict[str, Any]], ToolExecutionResult] | None = None,
    record_engineering_memory: Callable[[dict[str, Any]], ToolExecutionResult] | None = None,
    retrieve_code_context: Callable[[dict[str, Any]], ToolExecutionResult] | None = None,
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
        if call.name == "retrieve_code_context":
            if retrieve_code_context is None:
                raise BusinessException("当前阶段不能使用代码 RAG")
            return retrieve_code_context(args)
        if call.name == "edit_file_by_replace":
            return file_tools.edit_file_by_replace(
                workspace_root,
                path=str(args.get("path") or ""),
                old_text=str(args.get("old_text") or ""),
                new_text=str(args.get("new_text") or ""),
                expected_hash=str(args.get("expected_hash") or ""),
                tool_call_id=call.id,
            )
        if call.name == "write_new_code":
            if write_new_code is None:
                raise BusinessException("当前阶段不能生成完整文件")
            return write_new_code(args)
        if call.name == "record_engineering_memory":
            if record_engineering_memory is None:
                raise BusinessException("当前阶段不能记录工程记忆")
            return record_engineering_memory(args)
        if call.name == "install_project_dependency":
            return project_deps.install_project_dependency(
                workspace_root,
                manager=str(args.get("manager") or ""),
                action=str(args.get("action") or ""),
                packages=args.get("packages"),
                target=str(args.get("target") or "frontend"),
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
