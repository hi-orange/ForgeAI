"""Shared protocol helpers for bounded, single-tool agent loops."""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from typing import Any, Protocol

from app.core.exceptions import BusinessException
from app.schemas.agent_action import (
    ChatWithToolsResult,
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
)

MAX_TOOL_RESULT_CHARS = 20_000


class ToolModelCall(Protocol):
    def __call__(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> ChatWithToolsResult: ...


def append_tool_batch_exchange(
    messages: list[dict[str, Any]],
    *,
    result: ChatWithToolsResult,
    calls: list[ToolCall],
    observations: list[ToolExecutionResult],
) -> None:
    """Append one assistant batch and a result for every proposed native tool call."""

    messages.append(
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
                for call in calls
            ],
        }
    )
    messages.extend(
        {
            "role": "tool",
            "tool_call_id": observation.tool_call_id,
            "name": observation.name,
            "content": json.dumps(observation.model_dump(mode="json"), ensure_ascii=False)[
                :MAX_TOOL_RESULT_CHARS
            ],
        }
        for observation in observations
    )


def run_bounded_tool_loop[AgentOutputT](
    *,
    messages: list[dict[str, Any]],
    tools: list[ToolDefinition],
    allowed_tools: Collection[str],
    role_name: str,
    max_turns: int,
    temperature: float,
    max_tokens: int,
    missing_message: str,
    exhausted_message: str,
    execute_tool: Callable[[ToolCall], tuple[ToolExecutionResult, AgentOutputT | None]],
    model_call: ToolModelCall,
    on_observation: Callable[[ToolExecutionResult], None] | None = None,
) -> AgentOutputT:
    """Run a bounded loop while executing at most one proposed tool per model turn.

    Some OpenAI-compatible providers ignore ``parallel_tool_calls=False`` and still
    return a batch. The first call is executed and every extra call receives a
    provider-compatible deferred result. This keeps the role bounded without
    turning a harmless provider mismatch into a failed project execution.
    """

    history = list(messages)
    for _turn in range(max_turns):
        result = model_call(
            messages=list(history),
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if not result.tool_calls:
            raise BusinessException(missing_message)
        invalid = [call.name for call in result.tool_calls if call.name not in allowed_tools]
        if invalid:
            raise BusinessException(f"{role_name} 无权使用工具：{invalid[0]}")
        call = result.tool_calls[0]
        observation, output = execute_tool(call)
        if on_observation is not None:
            on_observation(observation)
        if output is not None:
            return output
        observations = [observation]
        for deferred in result.tool_calls[1:]:
            deferred_observation = ToolExecutionResult(
                tool_call_id=deferred.id,
                name=deferred.name,
                ok=False,
                error_code="TOOL_CALL_DEFERRED",
                summary="本轮只执行第一个工具；请根据结果逐步继续，不要并行调用工具。",
                arguments=dict(deferred.arguments),
            )
            observations.append(deferred_observation)
        append_tool_batch_exchange(
            history,
            result=result,
            calls=result.tool_calls,
            observations=observations,
        )
    raise BusinessException(exhausted_message)
