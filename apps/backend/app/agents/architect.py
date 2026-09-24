"""Architect tool loop for producing a validated system design."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.agents.prompts.architect import ARCHITECT_SYSTEM_PROMPT
from app.agents.roles import get_role_profile
from app.agents.tool_protocol import run_bounded_tool_loop
from app.core.exceptions import BusinessException
from app.core.llm import chat_with_tools
from app.models.task import TaskRecipient
from app.schemas.agent_action import ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.tools.architect import (
    ARCHITECT_TOOLS,
    ArchitectToolState,
    execute_architect_tool,
)

MAX_ARCHITECT_TOOL_TURNS = 10
ARCHITECT_PROFILE = get_role_profile(TaskRecipient.ARCHITECT)
ALLOWED_ARCHITECT_TOOLS = [
    tool for tool in ARCHITECT_TOOLS if tool.name in ARCHITECT_PROFILE.allowed_tools
]


def _initial_messages(spec: AppSpec) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": f"{ARCHITECT_SYSTEM_PROMPT}\n角色目标：{ARCHITECT_PROFILE.goal}",
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "frozen_product_intent": spec.model_dump(mode="json"),
                    "required_output": "system_design",
                },
                ensure_ascii=False,
            ),
        },
    ]


def generate_system_design(
    *,
    spec: AppSpec,
    workspace_root: Path,
    on_activity: Callable[[ToolExecutionResult], None] | None = None,
) -> SystemDesign:
    """Use bounded editor/terminal tools; persistence remains the caller's responsibility."""

    spec = AppSpec.model_validate(spec.model_dump())
    root = workspace_root.resolve()
    if not root.is_dir():
        raise BusinessException("Architect 工作区不存在")
    state = ArchitectToolState(app_spec=spec, workspace_root=root)
    return run_bounded_tool_loop(
        messages=_initial_messages(spec),
        tools=ALLOWED_ARCHITECT_TOOLS,
        allowed_tools=ARCHITECT_PROFILE.allowed_tools,
        role_name="Architect",
        max_turns=MAX_ARCHITECT_TOOL_TURNS,
        temperature=0.1,
        max_tokens=8192,
        missing_message="Architect 未调用工具提交系统设计",
        exhausted_message="Architect 工具调用预算已用尽，尚未提交系统设计",
        execute_tool=lambda call: execute_architect_tool(call, state),
        model_call=chat_with_tools,
        on_observation=on_activity,
    )
