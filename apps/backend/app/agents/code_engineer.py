"""Code Engineer model turn: choose a tool against frozen requirements."""

from __future__ import annotations

import json
from typing import Any

from app.agents.prompts.code_engineer import (
    CODE_ENGINEER_SYSTEM_PROMPT,
    CODE_WRITER_SYSTEM_PROMPT,
)
from app.agents.roles import get_role_profile
from app.core.exceptions import BusinessException
from app.core.llm import chat_completion, chat_with_tools
from app.generation.delivery import WorkItem
from app.models.task import TaskRecipient
from app.schemas.agent_action import ChatWithToolsResult, ToolDefinition, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.tools.code_engineer import CODE_ENGINEER_TOOLS as ALL_CODE_ENGINEER_TOOLS

CODE_ENGINEER_PROFILE = get_role_profile(TaskRecipient.CODE_ENGINEER)
CODE_ENGINEER_TOOLS = [
    tool for tool in ALL_CODE_ENGINEER_TOOLS if tool.name in CODE_ENGINEER_PROFILE.allowed_tools
]


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
    """Generate exactly one complete file from the controller's bounded context."""

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
    result = chat_with_tools(
        messages=build_code_engineer_messages(
            spec=spec,
            work_item=work_item,
            engineering_context=engineering_context,
            observations=observations,
            system_design=system_design,
        ),
        tools=tools or CODE_ENGINEER_TOOLS,
    )
    # The workflow deliberately executes one observed action per turn. Some compatible
    # providers ignore parallel_tool_calls=False, so fence extras here instead of executing
    # actions that were planned against stale observations.
    if len(result.tool_calls) > 1:
        return result.model_copy(update={"tool_calls": result.tool_calls[:1]})
    return result
