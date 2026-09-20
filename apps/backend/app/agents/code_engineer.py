"""Code Engineer model turn: choose a tool against frozen requirements."""

from __future__ import annotations

import json
from typing import Any

from app.agents.prompts.code_engineer import CODE_ENGINEER_SYSTEM_PROMPT
from app.agents.roles import get_role_profile
from app.core.llm import chat_with_tools
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


def build_code_engineer_messages(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    engineering_context: dict[str, Any],
    observations: list[ToolExecutionResult],
    system_design: SystemDesign,
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
                    "system_design": system_design.model_dump(mode="json"),
                    "engineering_context": engineering_context,
                },
                ensure_ascii=False,
            ),
        },
    ]
    for observation in observations[-8:]:
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
    system_design: SystemDesign,
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
