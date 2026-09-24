"""Product Manager tool loop for producing a validated, approval-ready PRD."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.agents.prompts.product_manager import APP_SPEC_SYSTEM_PROMPT
from app.agents.roles import get_role_profile
from app.agents.tool_protocol import run_bounded_tool_loop
from app.core.exceptions import BusinessException
from app.core.llm import chat_with_tools
from app.models.task import TaskRecipient
from app.schemas.app_spec import AppSpec
from app.schemas.product_manager import ProductManagerInput
from app.tools.product_manager import (
    PRODUCT_MANAGER_TOOLS,
    ProductManagerToolState,
    execute_product_manager_tool,
)

MAX_PRODUCT_MANAGER_TOOL_TURNS = 8
PRODUCT_MANAGER_PROFILE = get_role_profile(TaskRecipient.PRODUCT_MANAGER)
ALLOWED_PRODUCT_MANAGER_TOOLS = [
    tool for tool in PRODUCT_MANAGER_TOOLS if tool.name in PRODUCT_MANAGER_PROFILE.allowed_tools
]


def _initial_messages(payload: ProductManagerInput) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (f"{APP_SPEC_SYSTEM_PROMPT}\n角色目标：{PRODUCT_MANAGER_PROFILE.goal}"),
        },
        {"role": "user", "content": payload.model_dump_json()},
    ]


def generate_app_spec(payload: ProductManagerInput) -> AppSpec:
    """Run the bounded research/editor loop without performing database writes."""

    try:
        payload = ProductManagerInput.model_validate(payload.model_dump())
    except ValidationError as exc:
        raise BusinessException("Product Manager 需求输入不符合要求") from exc

    state = ProductManagerToolState(payload=payload)
    return run_bounded_tool_loop(
        messages=_initial_messages(payload),
        tools=ALLOWED_PRODUCT_MANAGER_TOOLS,
        allowed_tools=PRODUCT_MANAGER_PROFILE.allowed_tools,
        role_name="Product Manager",
        max_turns=MAX_PRODUCT_MANAGER_TOOL_TURNS,
        temperature=0.0,
        max_tokens=8192,
        missing_message="Product Manager 未调用工具提交 PRD",
        exhausted_message="Product Manager 工具调用预算已用尽，尚未提交 PRD",
        execute_tool=lambda call: execute_product_manager_tool(call, state),
        model_call=chat_with_tools,
    )
