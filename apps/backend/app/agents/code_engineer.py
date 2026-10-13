"""One engineering model decision over frozen intent and real tool evidence."""

from __future__ import annotations

import json
from typing import Any

from app.agents.prompts.code_engineer import CODE_ENGINEER_SYSTEM_PROMPT
from app.agents.roles import get_role_profile
from app.agents.tool_protocol import append_tool_batch_exchange
from app.core.exceptions import BusinessException
from app.core.llm import chat_with_tools
from app.generation.delivery import WorkItem
from app.models.task import TaskRecipient
from app.schemas.agent_action import (
    ChatWithToolsResult,
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
)
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.tools.code_engineer import CODE_ENGINEER_TOOLS as ALL_CODE_ENGINEER_TOOLS

CODE_ENGINEER_PROFILE = get_role_profile(TaskRecipient.CODE_ENGINEER)
CODE_ENGINEER_TOOLS = [
    tool for tool in ALL_CODE_ENGINEER_TOOLS if tool.name in CODE_ENGINEER_PROFILE.allowed_tools
]


def _compact(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 6000:
        return value[:6000] + "\n[历史内容已截断；需要完整证据时请定向重新读取]"
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_compact(item) for item in value[:40]]
    return value


def build_code_engineer_messages(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    engineering_context: dict[str, Any],
    observations: list[ToolExecutionResult],
    system_design: SystemDesign | None,
) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": f"{CODE_ENGINEER_SYSTEM_PROMPT}\n{CODE_ENGINEER_PROFILE.contract}",
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "product_intent": spec.model_dump(mode="json"),
                    "current_work_item": work_item.model_dump(mode="json"),
                    "system_design": system_design.model_dump(mode="json")
                    if system_design
                    else None,
                    "engineering_context": engineering_context,
                },
                ensure_ascii=False,
            ),
        },
    ]
    # Compact values, never slice serialized JSON or break a tool-call/result pair.
    retained: list[ToolExecutionResult] = []
    chars = 0
    for observation in reversed(observations[-24:]):
        payload = _compact(observation.model_dump(mode="json"))
        size = len(json.dumps(payload, ensure_ascii=False))
        if chars + size > 100_000:
            break
        retained.append(ToolExecutionResult.model_validate(payload))
        chars += size
    for observation in reversed(retained):
        call = ToolCall(
            id=observation.tool_call_id, name=observation.name, arguments=observation.arguments
        )
        append_tool_batch_exchange(
            history,
            result=ChatWithToolsResult(tool_calls=[call]),
            calls=[call],
            observations=[observation],
        )
    return history


def decide_next_action(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    engineering_context: dict[str, Any],
    observations: list[ToolExecutionResult],
    system_design: SystemDesign | None,
    tools: list[ToolDefinition] | None = None,
) -> ChatWithToolsResult:
    available = CODE_ENGINEER_TOOLS if tools is None else tools
    result = chat_with_tools(
        messages=build_code_engineer_messages(
            spec=spec,
            work_item=work_item,
            engineering_context=engineering_context,
            observations=observations,
            system_design=system_design,
        ),
        tools=available,
        temperature=0.1,
        max_tokens=16384,
    )
    if not result.tool_calls:
        raise BusinessException("Code Engineer 未调用工具推进当前工作单元")
    allowed = {tool.name for tool in available} & CODE_ENGINEER_PROFILE.allowed_tools
    for call in result.tool_calls:
        if call.name not in allowed:
            raise BusinessException(f"Code Engineer 无权使用工具：{call.name}")
    # No speculative parallel writes: the next decision must consume the first result.
    return result.model_copy(update={"tool_calls": result.tool_calls[:1]})
