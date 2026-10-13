"""Typed, bounded tools for the engineering decision loop.

Mutating tools run on a staging workspace. Only the orchestration layer may
commit their bytes after rechecking the execution lease and original hashes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.exceptions import AppException
from app.generation.delivery import PlannedWorkItem
from app.schemas.agent_action import (
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
    inline_model_json_schema,
)
from app.tools import checks, files, images, project_deps


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _List(_Arguments):
    path: str = "."


class _Read(_Arguments):
    path: str = Field(min_length=1)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class _Search(_Arguments):
    query: str = Field(min_length=1, max_length=500)
    path_filter: str | None = None


class _Write(_Arguments):
    path: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=262144)
    expected_hash: str | None = None


class _Edit(_Arguments):
    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str
    expected_hash: str = Field(min_length=1)


class _Image(_Arguments):
    path: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=4000)
    size: Literal["1K", "2K", "4K"] = "2K"
    watermark: bool = True


class _Dependency(_Arguments):
    manager: Literal["npm", "uv"]
    action: Literal["add", "remove"]
    packages: list[str] = Field(min_length=1, max_length=8)
    target: Literal["frontend", "backend"]


class _Check(_Arguments):
    check_id: Literal["quick", "database", "backend", "frontend", "all"]


class _Complete(_Arguments):
    work_item_id: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=2000)


class _Blocked(_Arguments):
    reason: str = Field(min_length=1, max_length=2000)


class _Plan(_Arguments):
    items: list[PlannedWorkItem] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)


_TOOL_ARGUMENTS: dict[str, type[_Arguments]] = {
    "update_delivery_plan": _Plan,
    "list_files": _List,
    "read_file": _Read,
    "search_code": _Search,
    "write_file": _Write,
    "edit_file_by_replace": _Edit,
    "generate_image": _Image,
    "install_project_dependency": _Dependency,
    "run_check": _Check,
    "complete_work_item": _Complete,
    "report_blocked": _Blocked,
}
_DESCRIPTIONS = {
    "update_delivery_plan": (
        "提交完整工程依赖图或调整剩余计划；先建立/验证视觉基础，再依赖它实现功能。"
        "需求可拆分，基础可共用；保留已完成项的全部定义，不提交 status/summary。"
        "平台校验覆盖、依赖及完成记录后，只执行依赖已满足的任务。"
    ),
    "list_files": "列出生成工作区文件；仅在不知道路径时使用。",
    "read_file": "读取真实源码及 content_hash；修改已有文件之前必须读取。",
    "search_code": "按准确符号或错误文本定位源码，返回路径和行号。",
    "write_file": (
        "创建文件或必要的整体重写；直接提供完整源码 content，已有文件必"
        "须提供读取时的 expected_hash。"
    ),
    "edit_file_by_replace": (
        "局部修改：old_text 必须唯一匹配，expected_hash 必须来自读取；冲突后重新读取。"
    ),
    "generate_image": ("生成图片到 frontend/public/；返回实际路径，只有配置图片服务时可用。"),
    "install_project_dependency": (
        "在隔离容器解析 npm/uv 项目依赖，更新清单和锁文件；禁止系统安装。"
    ),
    "run_check": "运行隔离检查获取证据；检查不等于完成任务，源码变化后旧证据失效。",
    "complete_work_item": (
        "请求平台校验并完成当前工作项；平台自动执行 quick，最后一项还执行"
        " all，失败返回证据继续修复。"
    ),
    "report_blocked": "缺少必要信息或当前约束无法满足时报告事实；不能伪造完成。",
}
CODE_ENGINEER_TOOLS = [
    ToolDefinition(
        name=name, description=_DESCRIPTIONS[name], parameters=inline_model_json_schema(model)
    )
    for name, model in _TOOL_ARGUMENTS.items()
]
ALLOWED_TOOL_NAMES = frozenset(_TOOL_ARGUMENTS)
MUTATING_TOOLS = frozenset(
    {"write_file", "edit_file_by_replace", "generate_image", "install_project_dependency"}
)


def validate_tool_call(call: ToolCall) -> ToolCall:
    model = _TOOL_ARGUMENTS.get(call.name)
    if model is None:
        raise ValueError(f"Code Engineer 无权使用工具：{call.name}")
    return call.model_copy(update={"arguments": model.model_validate(call.arguments).model_dump()})


def rejected_tool(call: ToolCall, error: Exception) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_call_id=call.id,
        name=call.name,
        ok=False,
        error_code="TOOL_REJECTED",
        summary=str(error)[:2000],
        arguments=call.arguments,
    )


def execute_tool_call(call: ToolCall, *, workspace_root: Path) -> ToolExecutionResult:
    """Execute an already scoped tool; mutations must target staging, never live source."""
    try:
        call = validate_tool_call(call)
        args = call.arguments
        handlers: dict[str, Callable[..., ToolExecutionResult]] = {
            "list_files": files.list_files,
            "read_file": files.read_file,
            "search_code": files.search_code,
            "write_file": files.apply_patch,
            "edit_file_by_replace": files.edit_file_by_replace,
            "generate_image": images.generate_project_image,
            "install_project_dependency": project_deps.install_project_dependency,
            "run_check": checks.run_check,
        }
        handler = handlers.get(call.name)
        if handler is None:
            raise ValueError("完成和阻断必须由工作流门禁处理")
        result = handler(workspace_root, **args, tool_call_id=call.id)
        return result.model_copy(update={"name": call.name, "arguments": call.arguments})
    except (AppException, ValidationError, ValueError, OSError) as exc:
        return rejected_tool(call, exc)
