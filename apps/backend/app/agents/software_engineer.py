"""SoftwareEngineer model turn: choose a tool against frozen requirements."""

from __future__ import annotations

import json
from typing import Any

from app.agents.prompts.software_engineer import (
    SOFTWARE_ENGINEER_PROMPT_VERSION,
    SOFTWARE_ENGINEER_SYSTEM_PROMPT,
)
from app.core.llm import chat_with_tools
from app.generation.delivery import WorkItem
from app.schemas.agent_action import ChatWithToolsResult, ToolDefinition, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.tools.registry import ENGINEERING_TOOLS

PROMPT_VERSION = SOFTWARE_ENGINEER_PROMPT_VERSION


def build_software_engineer_messages(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    observations: list[ToolExecutionResult],
) -> list[dict[str, Any]]:
    """OpenAI/DeepSeek tool history: each tool result follows its assistant tool_calls."""
    history: list[dict[str, Any]] = [
        {"role": "system", "content": SOFTWARE_ENGINEER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "goal": spec.goal,
                    "constraints": [item.model_dump(mode="json") for item in spec.constraints],
                    "current_work_item": work_item.model_dump(mode="json"),
                    "features": [item.model_dump(mode="json") for item in spec.features],
                    "acceptance_criteria": [
                        item.model_dump(mode="json") for item in spec.acceptance_criteria
                    ],
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
    observations: list[ToolExecutionResult],
    tools: list[ToolDefinition] | None = None,
) -> ChatWithToolsResult:
    return chat_with_tools(
        messages=build_software_engineer_messages(
            spec=spec, work_item=work_item, observations=observations
        ),
        tools=tools or ENGINEERING_TOOLS,
    )
