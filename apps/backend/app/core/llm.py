"""Thin OpenAI-compatible chat client (DeepSeek)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx
from pydantic import ValidationError

from app.core.exceptions import BusinessException, ModelProtocolException, ProviderException
from app.core.llm_response import CompletionContentKind, normalize_completion_content
from app.core.settings import settings
from app.schemas.agent_action import ChatWithToolsResult, TokenUsage, ToolCall, ToolDefinition
from app.telemetry.agent import AgentLogFilter, record_model_turn

logger = logging.getLogger("forgeai.llm")
logger.addFilter(AgentLogFilter())


def _text_length(value: object) -> int | None:
    """返回字符串长度；非字符串返回 None。"""
    return len(value) if isinstance(value, str) else None


def _content_preview(value: object, *, limit: int = 160) -> str:
    """截取内容预览，便于日志诊断空响应。"""
    if not isinstance(value, str):
        return repr(value)
    return repr(value[:limit])


def _http_error_message(response: httpx.Response | None) -> str:
    """Map provider HTTP failures to actionable user-facing copy."""

    status = response.status_code if response is not None else None
    body = (response.text if response is not None else "") or ""
    lowered = body.lower()

    if status == 401 or "invalid api key" in lowered or "authentication" in lowered:
        return "大模型鉴权失败，请检查 DEEPSEEK_API_KEY 是否正确"
    if status == 402 or "insufficient balance" in lowered or "insufficient_balance" in lowered:
        return "大模型账户余额不足，请前往 DeepSeek 控制台充值后再试"
    if "quota" in lowered and ("exceed" in lowered or "exhausted" in lowered):
        return "大模型账户余额不足，请前往 DeepSeek 控制台充值后再试"
    if status == 429 or "rate limit" in lowered or "too many requests" in lowered:
        return "大模型请求过于频繁，请稍后再试"
    if status is not None and status >= 500:
        return "大模型服务暂时不可用，请稍后重试"
    return "大模型调用失败，请稍后重试"


def _http_error_retryable(response: httpx.Response | None) -> bool:
    """Return whether replaying the identical provider request may recover."""

    status = response.status_code if response is not None else None
    body = (response.text if response is not None else "") or ""
    lowered = body.lower()

    if status in {401, 402} or "invalid api key" in lowered or "authentication" in lowered:
        return False
    if "insufficient balance" in lowered or "insufficient_balance" in lowered:
        return False
    if "quota" in lowered and ("exceed" in lowered or "exhausted" in lowered):
        return False
    if status is None:
        return True
    return status == 429 or status >= 500


def _stream_error() -> ProviderException:
    return ProviderException("大模型流式响应不完整，请稍后重试", retryable=True)


def _sse_data(response: httpx.Response) -> Iterator[str]:
    """Read complete SSE data events, ignoring comments and other event fields."""
    lines: list[str] = []
    for line in response.iter_lines():
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            if value == "[DONE]":
                if lines:
                    yield "\n".join(lines)
                yield value
                return
            lines.append(value)
        elif not line and lines:
            yield "\n".join(lines)
            lines = []
    if lines:
        yield "\n".join(lines)


def _read_streamed_chat(response: httpx.Response) -> dict[str, Any]:
    """Reassemble one complete DeepSeek turn before exposing any tool call."""
    content: list[str] = []
    reasoning: list[str] = []
    saw_content = saw_reasoning = saw_done = False
    calls: dict[int, dict[str, Any]] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None

    for event in _sse_data(response):
        if event == "[DONE]":
            saw_done = True
            break
        try:
            chunk = json.loads(event)
        except json.JSONDecodeError as exc:
            raise _stream_error() from exc
        if not isinstance(chunk, dict):
            raise _stream_error()
        if isinstance(chunk.get("model"), str):
            model = chunk["model"]
        choices = chunk.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise _stream_error()
        choice = choices[0]
        if choice.get("index") != 0 or not isinstance(choice.get("delta"), dict):
            raise _stream_error()
        delta = choice["delta"]
        if finish_reason is not None and any(
            delta.get(key) for key in ("content", "reasoning_content", "tool_calls")
        ):
            raise _stream_error()
        for key, parts in (("content", content), ("reasoning_content", reasoning)):
            value = delta.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise _stream_error()
            parts.append(value)
            if key == "content":
                saw_content = True
            else:
                saw_reasoning = True
        fragments = delta.get("tool_calls")
        if fragments is None:
            fragments = []
        if not isinstance(fragments, list):
            raise _stream_error()
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise _stream_error()
            index = fragment.get("index")
            if type(index) is not int or index < 0:
                raise _stream_error()
            call = calls.setdefault(
                index, {"id": None, "type": None, "name": None, "arguments": []}
            )
            for key in ("id", "type"):
                value = fragment.get(key)
                if value is None or value == "":
                    continue
                if not isinstance(value, str) or (call[key] is not None and call[key] != value):
                    raise _stream_error()
                call[key] = value
            function = fragment.get("function")
            if function is None:
                function = {}
            if not isinstance(function, dict):
                raise _stream_error()
            name = function.get("name")
            if name is not None and name != "":
                if not isinstance(name, str) or (call["name"] is not None and call["name"] != name):
                    raise _stream_error()
                call["name"] = name
            arguments = function.get("arguments")
            if arguments is not None:
                if not isinstance(arguments, str):
                    raise _stream_error()
                call["arguments"].append(arguments)
        terminal = choice.get("finish_reason")
        if terminal is not None:
            if not isinstance(terminal, str) or finish_reason is not None:
                raise _stream_error()
            finish_reason = terminal
            final_usage = chunk.get("usage")
            if not isinstance(final_usage, dict):
                raise _stream_error()
            usage = final_usage

    if not saw_done or finish_reason is None or usage is None:
        raise _stream_error()
    tool_calls = []
    if finish_reason != "length":
        for index in sorted(calls):
            call = calls[index]
            if not call["id"] or call["type"] != "function" or not call["name"]:
                raise _stream_error()
            tool_calls.append(
                {
                    "id": call["id"],
                    "type": call["type"],
                    "function": {"name": call["name"], "arguments": "".join(call["arguments"])},
                }
            )
    return {
        "model": model,
        "usage": usage,
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "content": "".join(content) if saw_content else None,
                    "reasoning_content": "".join(reasoning) if saw_reasoning else None,
                    "tool_calls": tool_calls,
                },
            }
        ],
    }


def _post_chat(body: dict[str, object], *, timeout: float) -> dict[str, Any]:
    """向 DeepSeek chat/completions 发请求，统一处理超时与 HTTP 错误。"""
    if not settings.deepseek_api_key:
        raise ProviderException("未配置 DEEPSEEK_API_KEY，无法调用大模型", retryable=False)

    url = f"{settings.deepseek_base_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            if body.get("stream") is True:
                with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code >= 300:
                        response.read()
                    response.raise_for_status()
                    data = _read_streamed_chat(response)
            else:
                response = client.post(url, headers=headers, json=body)
                response.raise_for_status()
                data = response.json()
    except httpx.TimeoutException as exc:
        logger.warning("LLM request timed out")
        raise ProviderException(
            "大模型请求超时，请稍后重试",
            retryable=True,
        ) from exc
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300] if exc.response is not None else str(exc)
        status = exc.response.status_code if exc.response is not None else None
        logger.warning("LLM HTTP error status=%s: %s", status, detail)
        raise ProviderException(
            _http_error_message(exc.response),
            retryable=_http_error_retryable(exc.response),
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning("LLM network error: %s", exc)
        raise ProviderException(
            "无法连接大模型服务，请检查网络或代理设置后重试",
            retryable=True,
        ) from exc
    if not isinstance(data, dict):
        raise BusinessException("大模型返回格式异常")
    return data


def chat_completion(
    *,
    messages: list[dict[str, str]],
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = 120.0,
    json_output: bool = False,
    content_kind: CompletionContentKind = "text",
) -> str:
    """普通对话补全，并在 provider 边界规范化文本传输包装。"""
    body: dict[str, object] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # DeepSeek V4 enables thinking by default. File/code generations otherwise
        # spend the entire max_tokens budget on reasoning and return empty content
        # with finish_reason="length".
        "thinking": {"type": "disabled"},
    }
    if json_output:
        body["response_format"] = {"type": "json_object"}

    data = _post_chat(body, timeout=timeout)

    try:
        choice = data["choices"][0]
        message = choice["message"]
        content = message.get("content")
    except (KeyError, IndexError, TypeError) as exc:
        logger.error(
            "Unexpected LLM response shape: type=%s keys=%s",
            type(data).__name__,
            list(data.keys()) if isinstance(data, dict) else None,
        )
        raise BusinessException("大模型返回格式异常") from exc

    if not isinstance(content, str) or not content.strip():
        finish_reason = choice.get("finish_reason")
        reasoning_content = message.get("reasoning_content")
        usage = data.get("usage") if isinstance(data, dict) else None
        diagnostics = {
            "model": data.get("model", settings.deepseek_model),
            "finish_reason": finish_reason,
            "content_type": type(content).__name__,
            "content_length": _text_length(content),
            "content_preview": _content_preview(content),
            "reasoning_content_length": _text_length(reasoning_content),
            "usage": usage if isinstance(usage, dict) else None,
        }
        logger.warning("LLM returned empty final content: %s", diagnostics)
        raise BusinessException(
            (
                "大模型最终输出为空"
                f"（content={diagnostics['content_type']}, "
                f"长度={diagnostics['content_length']}, "
                f"reasoning长度={diagnostics['reasoning_content_length']}, "
                f"finish_reason={finish_reason}）"
            ),
            data=diagnostics,
        )

    logger.info(
        "LLM completion received: model=%s finish_reason=%s content_length=%s usage=%s",
        data.get("model", settings.deepseek_model),
        choice.get("finish_reason"),
        len(content),
        data.get("usage") if isinstance(data, dict) else None,
    )

    return normalize_completion_content(content, kind=content_kind)


def _parse_arguments(raw: object) -> dict[str, Any]:
    """把工具参数解析成 dict；已是对象则原样返回，空串视为 {}。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BusinessException("工具参数不是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise BusinessException("工具参数必须是 JSON 对象")
    return parsed


def _parse_call_arguments(raw: object, *, name: str, call_id: str) -> dict[str, Any]:
    """Keep a malformed native call identifiable without retaining its source body."""
    try:
        return _parse_arguments(raw)
    except BusinessException as exc:
        cause = exc.__cause__
        location = (
            {"line": cause.lineno, "column": cause.colno}
            if isinstance(cause, json.JSONDecodeError)
            else {}
        )
        raise ModelProtocolException(
            str(exc),
            data={
                "protocol_issue": "invalid_tool_arguments",
                "tool_name": name[:128],
                "tool_call_id": call_id[:128],
                "argument_length": len(raw) if isinstance(raw, str) else None,
                **location,
            },
        ) from exc


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """json.loads 的 object_pairs_hook：拒绝重复键，避免静默覆盖。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 字段重复")
        result[key] = value
    return result


def parse_agent_action(payload: dict[str, Any]) -> ChatWithToolsResult:
    """解析 chat completion：优先 native tool_calls，否则尝试 JSON 动作协议。"""
    try:
        choice = payload["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BusinessException("大模型返回格式异常") from exc

    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise BusinessException("大模型返回格式异常")
    usage_raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    usage = TokenUsage.model_validate(usage_raw) if usage_raw else None
    native_calls = message.get("tool_calls") or []
    parsed_calls: list[ToolCall] = []
    if isinstance(native_calls, list) and native_calls:
        for index, item in enumerate(native_calls):
            if not isinstance(item, dict):
                raise BusinessException("工具调用格式异常")
            function_raw = item.get("function")
            function: dict[str, Any] = function_raw if isinstance(function_raw, dict) else {}
            name = function.get("name") or item.get("name")
            call_id = item.get("id") or f"call_{index + 1}"
            if not isinstance(name, str) or not name.strip():
                raise BusinessException("工具调用缺少名称")
            parsed_calls.append(
                ToolCall(
                    id=str(call_id),
                    name=name.strip(),
                    arguments=_parse_call_arguments(
                        function.get("arguments", item.get("arguments")),
                        name=name.strip(),
                        call_id=str(call_id),
                    ),
                )
            )
        return ChatWithToolsResult(
            content=content,
            tool_calls=parsed_calls,
            finish_reason=choice.get("finish_reason"),
            model=payload.get("model"),
            usage=usage,
            protocol="native",
        )

    text = content.strip() if isinstance(content, str) else ""
    if not text:
        finish_reason = choice.get("finish_reason")
        raise BusinessException(
            "大模型最终输出为空且没有工具调用",
            data={"finish_reason": finish_reason},
        )
    try:
        action = json.loads(text, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, ValueError):
        return ChatWithToolsResult(
            content=content,
            tool_calls=[],
            finish_reason=choice.get("finish_reason"),
            model=payload.get("model"),
            usage=usage,
            protocol="json",
        )
    if not isinstance(action, dict):
        raise BusinessException("JSON 动作协议必须是对象")
    calls_raw = action.get("tool_calls")
    if calls_raw is None and action.get("name"):
        calls_raw = [action]
    if not isinstance(calls_raw, list):
        return ChatWithToolsResult(
            content=content,
            tool_calls=[],
            finish_reason=choice.get("finish_reason"),
            model=payload.get("model"),
            usage=usage,
            protocol="json",
        )
    for index, item in enumerate(calls_raw):
        if not isinstance(item, dict):
            raise BusinessException("JSON 动作协议格式异常")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise BusinessException("JSON 动作协议缺少工具名")
        parsed_calls.append(
            ToolCall(
                id=str(item.get("id") or f"call_{index + 1}"),
                name=name.strip(),
                arguments=_parse_arguments(item.get("arguments")),
            )
        )
    return ChatWithToolsResult(
        content=action.get("content") if isinstance(action.get("content"), str) else content,
        tool_calls=parsed_calls,
        finish_reason=choice.get("finish_reason"),
        model=payload.get("model"),
        usage=usage,
        protocol="json",
    )


def chat_with_tools(
    *,
    messages: list[dict[str, Any]],
    tools: list[ToolDefinition],
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: float = 120.0,
    thinking: bool = False,
    parallel_tool_calls: bool = False,
) -> ChatWithToolsResult:
    """带 tools 的一轮对话；允许只有 tool_calls、content 为空。"""
    body: dict[str, object] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
        "parallel_tool_calls": parallel_tool_calls,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ],
    }
    if thinking:
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = "high"
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        body.pop("temperature", None)
        # Legacy non-thinking exchanges have no reasoning to replay. Do not
        # mutate checkpoint-owned dictionaries or invent reasoning for them.
        body["messages"] = [
            {"reasoning_content": "", **message}
            if message.get("role") == "assistant"
            else dict(message)
            for message in messages
        ]
    started = time.perf_counter()
    data = _post_chat(body, timeout=timeout)
    # Meter the received response before parsing tool arguments. Invalid output is
    # still paid work; never silently omit it from the run's measured usage.
    usage = data.get("usage")
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    choice = choice if isinstance(choice, dict) else {}
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}
    measured = ChatWithToolsResult(
        model=data.get("model") if isinstance(data.get("model"), str) else None,
        usage=TokenUsage.model_validate(
            {key: value for key, value in usage.items() if type(value) is int and value >= 0}
        )
        if isinstance(usage, dict)
        else None,
        protocol="native" if message.get("tool_calls") else "json",
    )
    record_model_turn(measured, round((time.perf_counter() - started) * 1000))
    try:
        if choice.get("finish_reason") == "length":
            counts = usage if isinstance(usage, dict) else {}
            details = counts.get("completion_tokens_details")
            details = details if isinstance(details, dict) else {}
            reasoning_tokens = details.get("reasoning_tokens")
            completion_tokens = counts.get("completion_tokens")
            reasoning_exhausted = (
                type(reasoning_tokens) is int
                and type(completion_tokens) is int
                and 0 < completion_tokens <= reasoning_tokens
                and not message.get("tool_calls")
                and not message.get("content")
            )
            kind = "reasoning_budget" if reasoning_exhausted else "output_truncated"
            advice = (
                "供应商计数显示思考耗尽输出预算，尚无工具输出；"
                "请直接选择一个最小必要动作，避免重复分析。"
                if reasoning_exhausted
                else "响应达到输出上限；若工具参数被截断，请缩小下一次操作和参数体积。"
            )
            raise ModelProtocolException(
                "MODEL_OUTPUT_TRUNCATED：本轮全部工具均未执行。此前成功的工具结果保持有效。"
                + advice,
                data={
                    "finish_reason": "length",
                    "truncation_kind": kind,
                    "completion_tokens": completion_tokens,
                    "reasoning_tokens": reasoning_tokens,
                },
            )
        result = parse_agent_action(data)
        reasoning = message.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise BusinessException("模型 reasoning_content 格式异常")
        if thinking and (result.protocol != "native" or reasoning is None):
            raise BusinessException("思考工具响应缺少原生调用或 reasoning_content，未执行工具")
        result = result.model_copy(update={"reasoning_content": reasoning})
    except (BusinessException, ValidationError, TypeError, AttributeError) as exc:
        logger.warning(
            "LLM invalid tool response: model=%s finish_reason=%s usage=%s error=%s",
            measured.model,
            choice.get("finish_reason"),
            usage,
            exc,
        )
        if isinstance(exc, ModelProtocolException):
            raise
        raise ModelProtocolException(str(exc)) from exc
    logger.info(
        "LLM tool turn: model=%s finish_reason=%s tool_calls=%s content_length=%s "
        "thinking=%s reasoning_length=%s usage=%s",
        result.model or data.get("model"),
        result.finish_reason,
        [call.name for call in result.tool_calls],
        _text_length(result.content),
        thinking,
        _text_length(result.reasoning_content),
        result.usage.model_dump() if result.usage else None,
    )
    return result
