"""Shared bounded tool loops with explicit observations and recoverable history."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Collection
from copy import deepcopy
from typing import Any, Protocol

from pydantic import BaseModel

from app.core.exceptions import BusinessException, ModelProtocolException
from app.schemas.agent_action import (
    ChatWithToolsResult,
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
)
from app.telemetry.agent import AgentLogFilter

logger = logging.getLogger("forgeai.tools")
logger.addFilter(AgentLogFilter())


def log_tool_result(
    role_name: str, turn: int, call: ToolCall, observation: ToolExecutionResult
) -> None:
    """Correlate tool outcomes without logging source bodies or raw arguments."""
    logger.info(
        "tool result role=%s turn=%s tool=%s call_id=%s ok=%s error_code=%s args_hash=%s",
        role_name,
        turn,
        call.name,
        call.id,
        observation.ok,
        observation.error_code,
        hashlib.sha256(json.dumps(call.arguments, sort_keys=True).encode()).hexdigest()[:16],
    )


MAX_TOOL_RESULT_CHARS = 20_000


def tool_observation_json(observation: ToolExecutionResult) -> str:
    """Bound context without cutting through JSON syntax or pretending evidence is complete."""
    payload = observation.model_dump(mode="json")
    serialized = json.dumps(payload, ensure_ascii=False)
    if len(serialized) <= MAX_TOOL_RESULT_CHARS:
        return serialized
    excerpt = json.dumps(payload["data"], ensure_ascii=False)
    payload.update(arguments={}, summary=observation.summary[:1000], truncated=True)
    limit = MAX_TOOL_RESULT_CHARS // 2
    while True:
        payload["data"] = {"excerpt": excerpt[:limit], "notice": "工具结果已截断，必要时定向重读"}
        serialized = json.dumps(payload, ensure_ascii=False)
        if len(serialized) <= MAX_TOOL_RESULT_CHARS:
            return serialized
        limit //= 2


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
            "content": tool_observation_json(observation),
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
    final_turn_tools: Collection[str] | None = None,
    checkpoint: dict[str, Any] | None = None,
    on_checkpoint: Callable[[dict[str, Any]], None] | None = None,
    export_state: Callable[[], dict[str, Any]] | None = None,
    batch_read_tools: Collection[str] = (),
) -> AgentOutputT:
    """Execute bounded read batches or one state-changing tool and checkpoint the turn.

    Every extra mutation receives an explicit deferred result. Recovery retains
    completed exchanges and spent turns, including reservations before failed requests.
    """

    progress = deepcopy(checkpoint or {})
    history = list(progress.get("history") or messages)

    def save() -> None:
        progress["history"] = history
        if export_state is not None:
            progress["state"] = export_state()
        if on_checkpoint is not None:
            on_checkpoint(deepcopy(progress))

    for turn in range(int(progress.get("turns", 0)), max_turns):
        turn_tools = tools
        turn_allowed_tools = allowed_tools
        if final_turn_tools is not None and turn == max_turns - 1:
            final_tool_names = set(final_turn_tools)
            turn_tools = [tool for tool in tools if tool.name in final_tool_names]
            turn_allowed_tools = final_tool_names
        progress["turns"] = turn + 1
        save()  # Reserve paid work before calling the provider, including failed calls.
        try:
            result = model_call(
                messages=list(history),
                tools=turn_tools,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except ModelProtocolException as exc:
            history.append(
                {
                    "role": "user",
                    "content": f"上次响应未执行任何工具：{exc}。请修正工具参数后继续。",
                }
            )
            save()
            continue
        if not result.tool_calls:
            raise BusinessException(missing_message)
        invalid = [call.name for call in result.tool_calls if call.name not in turn_allowed_tools]
        if invalid:
            raise BusinessException(f"{role_name} 无权使用工具：{invalid[0]}")
        count = (
            min(16, len(result.tool_calls))
            if all(call.name in batch_read_tools for call in result.tool_calls)
            else 1
        )
        observations = []
        output = None
        for call in result.tool_calls[:count]:
            observation, output = execute_tool(call)
            log_tool_result(role_name, turn + 1, call, observation)
            if on_observation is not None:
                on_observation(observation)
            observations.append(observation)
        for deferred in result.tool_calls[count:]:
            deferred_observation = ToolExecutionResult(
                tool_call_id=deferred.id,
                name=deferred.name,
                ok=False,
                error_code="TOOL_CALL_DEFERRED",
                summary="该调用未执行；只读工具可批量调用，状态变更请根据前一个结果单独继续。",
                arguments=dict(deferred.arguments),
            )
            observations.append(deferred_observation)
        append_tool_batch_exchange(
            history,
            result=result,
            calls=result.tool_calls,
            observations=observations,
        )
        if isinstance(output, BaseModel):
            progress["output"] = output.model_dump(mode="json")
        save()
        if output is not None:
            return output
    raise BusinessException(exhausted_message)
