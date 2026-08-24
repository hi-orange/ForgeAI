"""Thin OpenAI-compatible chat client (DeepSeek)."""

from __future__ import annotations

import logging

import httpx

from app.core.exceptions import BusinessException
from app.core.settings import settings

logger = logging.getLogger("forgeai.llm")


def _text_length(value: object) -> int | None:
    return len(value) if isinstance(value, str) else None


def _content_preview(value: object, *, limit: int = 160) -> str:
    if not isinstance(value, str):
        return repr(value)
    return repr(value[:limit])


def chat_completion(
    *,
    messages: list[dict[str, str]],
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = 120.0,
    json_output: bool = False,
) -> str:
    if not settings.deepseek_api_key:
        raise BusinessException("未配置 DEEPSEEK_API_KEY，无法调用 Product Manager Agent")

    url = f"{settings.deepseek_base_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    body: dict[str, object] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_output:
        body["response_format"] = {"type": "json_object"}
        # DeepSeek V4 enables thinking by default. Large structured generations can
        # otherwise spend the entire max_tokens budget on reasoning and return an
        # empty final content with finish_reason="length".
        body["thinking"] = {"type": "disabled"}

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
        raise BusinessException("大模型调用失败，请稍后重试") from exc
    except httpx.HTTPError as exc:
        logger.exception("LLM network error")
        raise BusinessException("无法连接大模型服务") from exc

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
