"""Bounded market-research access through Tavily; never fetch target URLs directly."""

from __future__ import annotations

from typing import Any

import httpx

from app.core.exceptions import BusinessException
from app.core.settings import settings

MAX_RESEARCH_RESULTS = 6
MAX_RESEARCH_TEXT_CHARS = 12_000


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not settings.tavily_api_key:
        raise BusinessException("未配置 TAVILY_API_KEY，无法进行市场或竞品调研")
    url = f"{settings.tavily_base_url.rstrip('/')}/{path.lstrip('/')}"
    body = {"api_key": settings.tavily_api_key, **payload}
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, json=body)
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException as exc:
        raise BusinessException("市场调研请求超时") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise BusinessException("市场调研服务调用失败") from exc
    if not isinstance(data, dict):
        raise BusinessException("市场调研服务返回格式异常")
    return data


def enhanced_search(query: str) -> dict[str, Any]:
    data = _post(
        "search",
        {
            "query": query,
            "search_depth": "advanced",
            "max_results": MAX_RESEARCH_RESULTS,
            "include_answer": True,
            "include_raw_content": False,
        },
    )
    results: list[dict[str, str]] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": str(item.get("title") or "")[:300],
                "url": str(item.get("url") or "")[:2000],
                "content": str(item.get("content") or "")[:3000],
            }
        )
    return {
        "answer": str(data.get("answer") or "")[:4000],
        "results": results[:MAX_RESEARCH_RESULTS],
    }


def browser_open(url: str) -> dict[str, Any]:
    data = _post("extract", {"urls": [url], "extract_depth": "advanced"})
    results = data.get("results") or []
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise BusinessException("浏览器没有提取到可读正文")
    item = results[0]
    return {
        "url": str(item.get("url") or url)[:2000],
        "content": str(item.get("raw_content") or "")[:MAX_RESEARCH_TEXT_CHARS],
    }
