"""Thin OpenAI-compatible chat client (DeepSeek)."""

from __future__ import annotations

import logging

import httpx

from app.core.exceptions import BusinessException
from app.core.settings import settings

logger = logging.getLogger("forgeai.llm")


def chat_completion(
    *,
    messages: list[dict[str, str]],
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = 120.0,
) -> str:
    if not settings.deepseek_api_key:
        raise BusinessException("未配置 DEEPSEEK_API_KEY，无法调用 Product Manager Agent")

    url = f"{settings.deepseek_base_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
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
        raise BusinessException("大模型调用失败，请稍后重试") from exc
    except httpx.HTTPError as exc:
        logger.exception("LLM network error")
        raise BusinessException("无法连接大模型服务") from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.error("Unexpected LLM response shape: %s", data)
        raise BusinessException("大模型返回格式异常") from exc

    if not isinstance(content, str) or not content.strip():
        raise BusinessException("大模型未返回有效内容")

    return content.strip()
