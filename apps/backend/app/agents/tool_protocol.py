"""Shared protocol helpers for bounded, single-tool agent loops."""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

from app.core.exceptions import BusinessException
from app.schemas.agent_action import ChatWithToolsResult, ToolCall, ToolExecutionResult

MAX_TOOL_RESULT_CHARS = 20_000


def require_single_tool_call(
    result: ChatWithToolsResult,
    *,
    role_name: str,
    allowed_tools: Collection[str],
    missing_message: str,
) -> ToolCall:
    """Return the only proposed call and enforce the role's tool boundary."""

    if not result.tool_calls:
        raise BusinessException(missing_message)
    if len(result.tool_calls) != 1:
        raise BusinessException(f"{role_name} 每轮只能调用一个工具")
    call = result.tool_calls[0]
    if call.name not in allowed_tools:
        raise BusinessException(f"{role_name} 无权使用工具：{call.name}")
    return call


def append_tool_exchange(
    messages: list[dict[str, Any]],
    *,
    result: ChatWithToolsResult,
    call: ToolCall,
    observation: ToolExecutionResult,
) -> None:
    """Append one provider-compatible assistant/tool exchange to model history."""

    messages.extend(
        [
            {
                "role": "assistant",
                "content": result.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": json.dumps(observation.model_dump(mode="json"), ensure_ascii=False)[
                    :MAX_TOOL_RESULT_CHARS
                ],
            },
        ]
    )
