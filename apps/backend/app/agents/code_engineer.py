"""Code Engineer model turn: choose a tool against frozen requirements."""

from __future__ import annotations

import json
from typing import Any

from app.agents.prompts.code_engineer import (
    CODE_ENGINEER_SYSTEM_PROMPT,
    CODE_WRITER_SYSTEM_PROMPT,
    FILE_PLANNER_SYSTEM_PROMPT,
    REPAIR_PLANNER_SYSTEM_PROMPT,
)
from app.agents.roles import get_role_profile
from app.core.exceptions import BusinessException
from app.core.llm import chat_completion, chat_with_tools
from app.generation.delivery import ImplementationPlan, WorkItem
from app.models.task import TaskRecipient
from app.schemas.agent_action import ChatWithToolsResult, ToolDefinition, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.tools.code_engineer import CODE_ENGINEER_TOOLS as ALL_CODE_ENGINEER_TOOLS

CODE_ENGINEER_PROFILE = get_role_profile(TaskRecipient.CODE_ENGINEER)
CODE_ENGINEER_TOOLS = [
    tool for tool in ALL_CODE_ENGINEER_TOOLS if tool.name in CODE_ENGINEER_PROFILE.allowed_tools
]


def _unwrap_json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    if not text:
        raise BusinessException("文件级实施规划器没有返回合法 JSON")
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        sliced = text[start : end + 1].strip()
        if sliced not in candidates:
            candidates.append(sliced)
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(payload, dict):
            return payload
        last_error = ValueError("JSON root is not an object")
    raise BusinessException("文件级实施规划器没有返回合法 JSON") from last_error


def _planner_messages(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    workspace_file_index: dict[str, Any],
    system_design: SystemDesign | None,
    repair_context: dict[str, Any] | None,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                REPAIR_PLANNER_SYSTEM_PROMPT
                if repair_context is not None
                else FILE_PLANNER_SYSTEM_PROMPT
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "product_intent": spec.model_dump(mode="json"),
                    "current_work_item": work_item.model_dump(mode="json"),
                    "system_design": (
                        system_design.model_dump(mode="json") if system_design is not None else None
                    ),
                    "workspace_file_index": workspace_file_index,
                    "repair_context": repair_context,
                    "output_schema": ImplementationPlan.model_json_schema(),
                },
                ensure_ascii=False,
            ),
        },
    ]


def _validate_implementation_plan(raw: str, *, work_item: WorkItem) -> ImplementationPlan:
    try:
        plan = ImplementationPlan.model_validate(_unwrap_json_object(raw))
    except BusinessException:
        raise
    except ValueError as exc:
        raise BusinessException("文件级实施计划结构不合法") from exc
    if plan.work_item_id != work_item.id:
        raise BusinessException("文件级实施计划与当前工作单元不匹配")
    plan.validate_unique_paths()
    return plan


def plan_work_item_files(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    workspace_file_index: dict[str, Any],
    system_design: SystemDesign | None,
    repair_context: dict[str, Any] | None = None,
) -> ImplementationPlan:
    """Turn one approved work item into an ordered, validated file graph."""

    messages = _planner_messages(
        spec=spec,
        work_item=work_item,
        workspace_file_index=workspace_file_index,
        system_design=system_design,
        repair_context=repair_context,
    )
    raw = chat_completion(
        messages=list(messages),
        temperature=0.1,
        max_tokens=8192,
        json_output=True,
    )
    try:
        return _validate_implementation_plan(raw, work_item=work_item)
    except BusinessException:
        # One repair turn: models occasionally wrap JSON or omit required fields.
        messages.extend(
            [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "上一次输出不是符合 output_schema 的单个 JSON 对象。"
                        "请只重新输出完整 JSON，不要使用 Markdown 围栏或解释文字。"
                    ),
                },
            ]
        )
        raw = chat_completion(
            messages=list(messages),
            temperature=0.0,
            max_tokens=8192,
            json_output=True,
        )
        return _validate_implementation_plan(raw, work_item=work_item)


def _unwrap_file_content(value: str) -> str:
    content = value.strip()
    if content.startswith("```") and content.endswith("```"):
        first_newline = content.find("\n")
        if first_newline != -1:
            content = content[first_newline + 1 : -3].strip()
    if not content or "\x00" in content:
        raise BusinessException("写码模型没有返回有效文件内容")
    return content + ("" if content.endswith("\n") else "\n")


def generate_file_content(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    path: str,
    file_description: str,
    engineering_context: dict[str, Any],
    observations: list[ToolExecutionResult],
    system_design: SystemDesign | None,
) -> str:
    """Generate exactly one complete file from platform-assembled context."""

    recent_memory = [observation.model_dump(mode="json") for observation in observations]
    raw = chat_completion(
        messages=[
            {"role": "system", "content": CODE_WRITER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "goal": spec.goal,
                        "constraints": [item.model_dump(mode="json") for item in spec.constraints],
                        "current_work_item": work_item.model_dump(mode="json"),
                        "system_design": (
                            system_design.model_dump(mode="json")
                            if system_design is not None
                            else None
                        ),
                        "target_path": path,
                        "file_description": file_description,
                        "engineering_context": engineering_context,
                        "recent_memory": recent_memory,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        temperature=0.2,
        max_tokens=8192,
    )
    return _unwrap_file_content(raw)


def build_code_engineer_messages(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    engineering_context: dict[str, Any],
    observations: list[ToolExecutionResult],
    system_design: SystemDesign | None,
) -> list[dict[str, Any]]:
    """OpenAI/DeepSeek tool history: each tool result follows its assistant tool_calls."""
    history: list[dict[str, Any]] = [
        {"role": "system", "content": CODE_ENGINEER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "product_intent": {
                        "goal": spec.goal,
                        "constraints": [item.model_dump(mode="json") for item in spec.constraints],
                        "features": [item.model_dump(mode="json") for item in spec.features],
                    },
                    "current_work_item": work_item.model_dump(mode="json"),
                    "acceptance_criteria": [
                        item.model_dump(mode="json") for item in spec.acceptance_criteria
                    ],
                    "system_design": (
                        system_design.model_dump(mode="json") if system_design is not None else None
                    ),
                    "delivery_path": "designed" if system_design is not None else "direct",
                    "engineering_context": engineering_context,
                },
                ensure_ascii=False,
            ),
        },
    ]
    for observation in observations:
        history.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": observation.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": observation.name,
                            "arguments": json.dumps(observation.arguments, ensure_ascii=False),
                        },
                    }
                ],
            }
        )
        history.append(
            {
                "role": "tool",
                "tool_call_id": observation.tool_call_id,
                "name": observation.name,
                "content": json.dumps(observation.model_dump(mode="json"), ensure_ascii=False)[
                    :8000
                ],
            }
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
    available_tools = tools or CODE_ENGINEER_TOOLS
    result = chat_with_tools(
        messages=build_code_engineer_messages(
            spec=spec,
            work_item=work_item,
            engineering_context=engineering_context,
            observations=observations,
            system_design=system_design,
        ),
        tools=available_tools,
    )
    if not result.tool_calls:
        raise BusinessException("Code Engineer 未调用工具推进当前工作单元")
    call = result.tool_calls[0]
    if call.name not in {tool.name for tool in available_tools}:
        raise BusinessException(f"Code Engineer 无权使用工具：{call.name}")
    return result.model_copy(update={"tool_calls": [call]})
