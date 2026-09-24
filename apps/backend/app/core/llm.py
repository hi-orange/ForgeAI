"""Thin OpenAI-compatible chat client (DeepSeek)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.core.exceptions import BusinessException
from app.core.settings import settings
from app.schemas.agent_action import ChatWithToolsResult, TokenUsage, ToolCall, ToolDefinition

logger = logging.getLogger("forgeai.llm")


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


def _post_chat(body: dict[str, object], *, timeout: float) -> dict[str, Any]:
    """向 DeepSeek chat/completions 发请求，统一处理超时与 HTTP 错误。"""
    if not settings.deepseek_api_key:
        raise BusinessException("未配置 DEEPSEEK_API_KEY，无法调用大模型")

    url = f"{settings.deepseek_base_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException as exc:
        logger.exception("LLM request timed out")
        raise BusinessException("大模型请求超时，请稍后重试") from exc
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300] if exc.response is not None else str(exc)
        logger.exception("LLM HTTP error: %s", detail)
        raise BusinessException(_http_error_message(exc.response)) from exc
    except httpx.HTTPError as exc:
        logger.exception("LLM network error")
        raise BusinessException("无法连接大模型服务，请检查网络或代理设置后重试") from exc
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
) -> str:
    """普通对话补全；json_output 时强制 JSON。始终关闭 thinking，避免推理占满输出预算。"""
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

    return content.strip()


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
                    arguments=_parse_arguments(function.get("arguments", item.get("arguments"))),
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
) -> ChatWithToolsResult:
    """带 tools 的一轮对话；允许只有 tool_calls、content 为空。"""
    body: dict[str, object] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
        "parallel_tool_calls": False,
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
    data = _post_chat(body, timeout=timeout)
    result = parse_agent_action(data)
    logger.info(
        "LLM tool turn: model=%s finish_reason=%s tool_calls=%s content_length=%s usage=%s",
        result.model or data.get("model"),
        result.finish_reason,
        [call.name for call in result.tool_calls],
        _text_length(result.content),
        result.usage.model_dump() if result.usage else None,
    )
    return result
