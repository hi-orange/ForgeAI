"""Typed, bounded tools for the engineering decision loop.

Mutating tools run on a staging workspace. Only the orchestration layer may
commit their bytes after rechecking the execution lease and original hashes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.exceptions import AppException
from app.generation.delivery import DeliveryPlanError, PlannedWorkItem
from app.generation.engineering.diagnostics import feedback
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


class _BatchWrite(_Arguments):
    writes: list[_Write] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def bounded_payload(self) -> _BatchWrite:
        if sum(len(row.content.encode("utf-8")) for row in self.writes) > 262144:
            raise ValueError("批量源码总量不得超过 256 KiB，请按当前功能拆分")
        return self


class _GenerateClient(_Arguments):
    pass


class _Capability(_Arguments):
    capability_id: Literal["auth-session-v1"]


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
    items: list[PlannedWorkItem] | None = Field(default=None, min_length=1, max_length=100)
    suggested_plan_hash: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
        description="仅初始空计划可用：明确采纳上下文中已展示的完整建议图，提交其 content_hash。",
    )
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def one_plan_source(self) -> _Plan:
        if (self.items is None) == (self.suggested_plan_hash is None):
            raise ValueError("items 与 suggested_plan_hash 必须且只能提供一个")
        return self


_TOOL_ARGUMENTS: dict[str, type[_Arguments]] = {
    "update_delivery_plan": _Plan,
    "list_files": _List,
    "read_file": _Read,
    "search_code": _Search,
    "write_file": _Write,
    "write_files": _BatchWrite,
    "generate_api_client": _GenerateClient,
    "install_capability": _Capability,
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
        "初始空计划可用 suggested_plan_hash 明确采纳上下文中的完整建议图，免于转抄 items；"
        "二者只能选一个，已有计划的重规划必须提交完整 items。"
        "需求可拆分，基础可共用；保留已完成项的全部定义，不提交 status/summary。"
        "平台校验覆盖、依赖及完成记录后，只执行依赖已满足的任务。"
    ),
    "list_files": "列出生成工作区文件；仅在不知道路径时使用。",
    "read_file": (
        "读取当前工作区源码及 content_hash；仅在已有上下文缺少所需范围、内容截断或版本过期时调用。"
    ),
    "search_code": "按准确符号或错误文本定位源码，返回路径和行号。",
    "write_file": (
        "创建文件或必要的整体重写；直接提供完整源码 content。已有文件须提供当前已观察源码"
        "的 expected_hash；初始源码上下文或工作记忆中的完整、当前内容也可作为依据。"
    ),
    "edit_file_by_replace": (
        "局部修改：old_text 必须唯一匹配，expected_hash 须匹配当前已观察的源码；"
        "初始源码上下文或工作记忆中的完整、当前内容也可作为依据，冲突后定向重读。"
    ),
    "write_files": (
        "创建或整体改写 1～8 个当前工作项相关文件，总量最多 256 KiB；单文件也用本工具。"
        "已有文件需提供与当前已观察源码匹配的 expected_hash；初始源码上下文或工作记忆中的"
        "完整、当前内容也可作为依据，缺失或过期时才定向 read_file。"
        "所有文件校验后一起提交，冲突则整批不提交。"
        "局部单文件修改用 edit_file_by_replace。"
    ),
    "generate_image": ("生成图片到 frontend/public/；返回实际路径，只有配置图片服务时可用。"),
    "generate_api_client": (
        "从当前 FastAPI app.main:app 在隔离容器导出 OpenAPI；使用固定模板生成"
        " frontend/openapi.json 和 frontend/src/client/ 类型及 SDK。该目录完全由生成器管理，"
        "会移除旧生成文件；业务封装放 src/lib/。后端接口就绪或变更后调用，生成本身不代表检查通过。"
    ),
    "install_capability": (
        "安装冻结设计显式选择的版本化能力包；仅首次安装，不覆盖自定义业务或迁移。"
        "auth-session-v1 提供登录/退出/身份会话、迁移和 React 状态，无默认账户或自动注册。"
        "必须先读取包需修改的模板入口；安装后仍需实现获批业务授权并通过检查。"
    ),
    "install_project_dependency": (
        "在隔离容器解析 npm/uv 项目依赖，更新清单和锁文件；禁止系统安装。"
    ),
    "run_check": "运行隔离检查获取证据；检查不等于完成任务，源码变化后旧证据失效。",
    "complete_work_item": (
        "平台按实际源码变更选择 quick/frontend/all；业务和高风险变更执行 all，"
        "最终项、前置能力和 QA 修复必须 all。局部检查通过不能替代最终独立验收。"
    ),
    "report_blocked": "缺少必要信息或当前约束无法满足时报告事实；不能伪造完成。",
}
CODE_ENGINEER_TOOLS = [
    ToolDefinition(
        name=name, description=_DESCRIPTIONS[name], parameters=inline_model_json_schema(model)
    )
    for name, model in _TOOL_ARGUMENTS.items()
    # Accept old checkpoint calls, but expose only the unified write tool to the model.
    if name != "write_file"
]
ALLOWED_TOOL_NAMES = frozenset(_TOOL_ARGUMENTS)
MUTATING_TOOLS = frozenset(
    {
        "write_file",
        "write_files",
        "edit_file_by_replace",
        "generate_image",
        "install_project_dependency",
        "generate_api_client",
        "install_capability",
    }
)


def validate_tool_call(call: ToolCall) -> ToolCall:
    model = _TOOL_ARGUMENTS.get(call.name)
    if model is None:
        raise ValueError(f"Code Engineer 无权使用工具：{call.name}")
    return call.model_copy(update={"arguments": model.model_validate(call.arguments).model_dump()})


def rejected_tool(call: ToolCall, error: Exception) -> ToolExecutionResult:
    data = {}
    if isinstance(error, DeliveryPlanError):
        data = {
            "diagnostic": error.diagnostic.model_dump(mode="json"),
            "conflicts": error.diagnostic.issues,
        }
    elif isinstance(error, ValidationError):
        data = {
            "validation_errors": error.errors(
                include_input=False, include_context=False, include_url=False
            )
        }
    return feedback(
        ToolExecutionResult(
            tool_call_id=call.id,
            name=call.name,
            ok=False,
            error_code="TOOL_REJECTED",
            summary=str(error)[:2000],
            arguments=call.arguments,
            data=data,
        )
    )


def execute_tool_call(call: ToolCall, *, workspace_root: Path) -> ToolExecutionResult:
    """Execute an already scoped tool; mutations must target staging, never live source."""
    try:
        call = validate_tool_call(call)
        args = call.arguments
        if call.name in {"write_file", "write_files"}:
            writes = args["writes"] if call.name == "write_files" else [args]
            result = files.apply_patch_batch(
                workspace_root, writes=writes, revision_id="staged", tool_call_id=call.id
            )
            if call.name == "write_file" and result.ok:
                result.data.update(result.data["files"][0])
            return result.model_copy(update={"name": call.name, "arguments": args})
        handlers: dict[str, Callable[..., ToolExecutionResult]] = {
            "list_files": files.list_files,
            "read_file": files.read_file,
            "search_code": files.search_code,
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
