"""Tool contract and bounded execution state for Product Manager."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.core.exceptions import BusinessException
from app.schemas.agent_action import ToolCall, ToolDefinition, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.product_manager import ProductManagerInput
from app.tools import web_research

_PRD_SCHEMA = AppSpec.model_json_schema()

PRODUCT_MANAGER_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="enhanced_search",
        description=(
            "搜索公开市场、竞品和行业资料。只有用户要求调研，或 PRD 决策确实需要外部证据时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 2, "maxLength": 300}},
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="browser_open",
        description="打开增强搜索返回的公开页面，提取正文用于市场或竞品分析。",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "minLength": 8, "maxLength": 2000}},
            "required": ["url"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="search_product_context",
        description="在本任务固定的用户消息、历史对话和上一版 PRD 中增强搜索，不访问新消息。",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="edit_prd",
        description="在编辑器中保存一版完整 PRD 草稿。草稿仍可继续研究和修改，不会提交成果。",
        parameters={
            "type": "object",
            "properties": {"prd": _PRD_SCHEMA},
            "required": ["prd"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="write_prd",
        description="提交最终完整 PRD。只有目标、功能、约束和可观察验收条件一致后才调用。",
        parameters={
            "type": "object",
            "properties": {"prd": _PRD_SCHEMA},
            "required": ["prd"],
            "additionalProperties": False,
        },
    ),
]


@dataclass(slots=True)
class ProductManagerToolState:
    payload: ProductManagerInput
    draft: AppSpec | None = None
    opened_urls: set[str] = field(default_factory=set)


def _context_documents(payload: ProductManagerInput) -> list[dict[str, Any]]:
    documents = [
        {
            "source": f"message:{message.id}",
            "content": message.content,
        }
        for message in [*payload.recent_messages, payload.source_message]
    ]
    if payload.previous_app_spec is not None:
        documents.append(
            {
                "source": f"app_spec:{payload.previous_item_id}",
                "content": payload.previous_app_spec.model_dump_json(),
            }
        )
    return documents


def _search_context(payload: ProductManagerInput, query: str) -> list[dict[str, str]]:
    terms = [term.casefold() for term in query.split() if term.strip()]
    hits: list[dict[str, str]] = []
    for document in _context_documents(payload):
        content = str(document["content"])
        folded = content.casefold()
        if terms and not all(term in folded for term in terms):
            continue
        hits.append({"source": str(document["source"]), "content": content[:3000]})
    return hits[:10]


def _prd_from_call(call: ToolCall) -> AppSpec:
    try:
        return AppSpec.model_validate(call.arguments.get("prd"))
    except ValidationError as exc:
        raise BusinessException("PRD 不符合 app_spec 结构或缺少可观察验收条件") from exc


def execute_product_manager_tool(
    call: ToolCall,
    state: ProductManagerToolState,
) -> tuple[ToolExecutionResult, AppSpec | None]:
    try:
        if call.name == "enhanced_search":
            query = str(call.arguments.get("query") or "").strip()
            if not query:
                raise BusinessException("增强搜索关键字不能为空")
            data = web_research.enhanced_search(query)
            state.opened_urls.update(
                str(item.get("url"))
                for item in data.get("results") or []
                if isinstance(item, dict) and item.get("url")
            )
            result = ToolExecutionResult(
                tool_call_id=call.id,
                name=call.name,
                ok=True,
                summary=f"已完成增强搜索：{query}",
                data=data,
            )
            return result.model_copy(update={"arguments": call.arguments}), None
        if call.name == "browser_open":
            url = str(call.arguments.get("url") or "").strip()
            if url not in state.opened_urls:
                raise BusinessException("只能打开本次增强搜索返回的公开页面")
            data = web_research.browser_open(url)
            result = ToolExecutionResult(
                tool_call_id=call.id,
                name=call.name,
                ok=True,
                summary="已提取公开页面正文",
                data=data,
            )
            return result.model_copy(update={"arguments": call.arguments}), None
        if call.name == "search_product_context":
            query = str(call.arguments.get("query") or "").strip()
            if not query:
                raise BusinessException("产品上下文搜索关键字不能为空")
            hits = _search_context(state.payload, query)
            result = ToolExecutionResult(
                tool_call_id=call.id,
                name=call.name,
                ok=True,
                summary=f"固定产品上下文命中 {len(hits)} 条",
                data={"hits": hits},
            )
            return result.model_copy(update={"arguments": call.arguments}), None
        if call.name in {"edit_prd", "write_prd"}:
            prd = _prd_from_call(call)
            state.draft = prd
            result = ToolExecutionResult(
                tool_call_id=call.id,
                name=call.name,
                ok=True,
                summary="已保存 PRD 草稿" if call.name == "edit_prd" else "已提交最终 PRD",
                data={"goal": prd.goal, "feature_count": len(prd.features)},
            )
            return result.model_copy(update={"arguments": call.arguments}), (
                prd if call.name == "write_prd" else None
            )
        raise BusinessException(f"Product Manager 无权使用工具：{call.name}")
    except BusinessException as exc:
        return (
            ToolExecutionResult(
                tool_call_id=call.id,
                name=call.name,
                ok=False,
                error_code="TOOL_REJECTED",
                summary=str(exc),
                arguments=dict(call.arguments),
            ),
            None,
        )
