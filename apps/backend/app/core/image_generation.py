"""Volcengine Ark image generation client used by the engineering pipeline."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.core.exceptions import BusinessException
from app.core.settings import settings

logger = logging.getLogger("forgeai.image_generation")


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    data: bytes
    media_type: str
    source: str


def _provider_error(response: httpx.Response | None) -> str:
    status = response.status_code if response is not None else None
    body = (response.text if response is not None else "").lower()
    if status == 401 or "invalid api key" in body or "authentication" in body:
        return "图片生成鉴权失败，请检查 ARK_API_KEY"
    if status == 429 or "rate limit" in body:
        return "图片生成请求过于频繁，请稍后再试"
    if status is not None and status >= 500:
        return "图片生成服务暂时不可用，请稍后再试"
    return "图片生成失败，请检查 Ark 模型和账户配置"


def _safe_remote_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BusinessException("图片生成服务没有返回可下载图片")
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise BusinessException("图片生成服务返回了不安全的下载地址")
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise BusinessException("图片生成服务返回了不安全的下载地址")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise BusinessException("图片生成服务返回了不安全的下载地址")
    return url


def _decode_image_response(payload: dict[str, object], client: httpx.Client) -> GeneratedImage:
    rows = payload.get("data")
    first = rows[0] if isinstance(rows, list) and rows else None
    if not isinstance(first, dict):
        raise BusinessException("图片生成服务返回格式异常")
    encoded = first.get("b64_json")
    if isinstance(encoded, str) and encoded:
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BusinessException("图片生成服务返回了无效图片数据") from exc
        return _validated_image(data, content_type=None, source="base64")

    url = _safe_remote_url(first.get("url"))
    try:
        response = client.get(url, follow_redirects=False)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise BusinessException("下载生成图片超时，请稍后重试") from exc
    except httpx.HTTPError as exc:
        raise BusinessException("无法下载图片生成结果，请稍后重试") from exc
    return _validated_image(
        response.content,
        content_type=response.headers.get("content-type"),
        source=url,
    )


def _validated_image(data: bytes, *, content_type: str | None, source: str) -> GeneratedImage:
    if not data or len(data) > settings.image_generation_max_bytes:
        raise BusinessException("生成图片为空或超过平台大小限制")
    signatures = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"RIFF", "image/webp"),
    )
    media_type = next((kind for signature, kind in signatures if data.startswith(signature)), None)
    if media_type == "image/webp" and data[8:12] != b"WEBP":
        media_type = None
    if media_type is None:
        raise BusinessException("图片生成服务返回的不是受支持的 PNG、JPEG 或 WebP")
    if content_type and not (
        content_type.lower().startswith("image/")
        or content_type.lower().startswith("application/octet-stream")
    ):
        raise BusinessException("图片下载响应类型异常")
    return GeneratedImage(data=data, media_type=media_type, source=source)


def generate_image(prompt: str, *, size: str = "2K", watermark: bool = True) -> GeneratedImage:
    """Generate one image through Ark's OpenAI-compatible images endpoint."""

    clean_prompt = prompt.strip()
    if not clean_prompt:
        raise BusinessException("图片描述不能为空")
    if len(clean_prompt) > 4000:
        raise BusinessException("图片描述过长")
    if size not in {"1K", "2K", "4K"}:
        raise BusinessException("图片尺寸只支持 1K、2K 或 4K")
    if not settings.image_generation_enabled:
        raise BusinessException("未配置 ARK_API_KEY，图片生成功能未启用")

    url = f"{settings.ark_base_url.rstrip('/')}/images/generations"
    headers = {
        "Authorization": f"Bearer {settings.ark_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.ark_image_model,
        "prompt": clean_prompt,
        "size": size,
        "response_format": "url",
        "watermark": watermark,
    }
    try:
        with httpx.Client(timeout=settings.image_generation_timeout_seconds) as client:
            response = client.post(url, headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise BusinessException("图片生成服务返回格式异常")
            return _decode_image_response(payload, client)
    except httpx.TimeoutException as exc:
        raise BusinessException("图片生成请求超时，请稍后重试") from exc
    except httpx.HTTPStatusError as exc:
        logger.exception("Ark image generation HTTP error: %s", exc.response.text[:300])
        raise BusinessException(_provider_error(exc.response)) from exc
    except httpx.HTTPError as exc:
        logger.exception("Ark image generation network error")
        raise BusinessException("无法连接图片生成服务，请检查网络或代理设置") from exc
    except ValueError as exc:
        raise BusinessException("图片生成服务返回格式异常") from exc
