"""One engineering model decision over frozen intent and real tool evidence."""

from __future__ import annotations

import json
from typing import Any

from app.agents.prompts.code_engineer import CODE_ENGINEER_SYSTEM_PROMPT
from app.agents.roles import get_role_profile
from app.agents.tool_protocol import append_tool_batch_exchange
from app.core.exceptions import ModelProtocolException
from app.core.llm import chat_with_tools
from app.generation.delivery import WorkItem
from app.models.task import TaskRecipient
from app.schemas.agent_action import (
    ChatWithToolsResult,
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
)
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.tools.code_engineer import CODE_ENGINEER_TOOLS as ALL_CODE_ENGINEER_TOOLS

CODE_ENGINEER_PROFILE = get_role_profile(TaskRecipient.CODE_ENGINEER)
CODE_ENGINEER_TOOLS = [
    tool for tool in ALL_CODE_ENGINEER_TOOLS if tool.name in CODE_ENGINEER_PROFILE.allowed_tools
]


def _compact(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 6000:
        return value[:6000] + "\n[历史内容已截断；需要完整证据时请定向重新读取]"
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_compact(item) for item in value[:40]]
    return value


def build_code_engineer_messages(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    engineering_context: dict[str, Any],
    observations: list[ToolExecutionResult],
    system_design: SystemDesign | None,
) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": f"{CODE_ENGINEER_SYSTEM_PROMPT}\n{CODE_ENGINEER_PROFILE.contract}",
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "product_intent": spec.model_dump(mode="json"),
                    "system_design": system_design.model_dump(mode="json")
                    if system_design
                    else None,
                },
                ensure_ascii=False,
            ),
        },
    ]
    # Compact values, never slice serialized JSON or break a tool-call/result pair.
    retained: list[ToolExecutionResult] = []
    chars = 0
    seen: set[str] = set()
    memory = engineering_context.get("working_memory") or {}
    evidence_hashes = {
        entry["path"]: entry["content_hash"] for entry in memory.get("source_evidence", [])
    }
    evidence_paths = set(evidence_hashes)
    stale_paths = set(memory.get("stale_evidence_paths", []))
    retained_reads: set[str] = set()
    for observation in reversed(observations[-24:]):
        if observation.name == "read_file":
            path = observation.data.get("path")
            if (
                path in stale_paths
                or (path in evidence_paths and path in retained_reads)
                or (
                    path in evidence_hashes
                    and observation.data.get("content_hash") != evidence_hashes[path]
                )
            ):
                continue
            if isinstance(path, str):
                retained_reads.add(path)
            # Keep the latest native read exchange: source memory supplements the
            # immediate action/result feedback, it must not erase it.
        # Keep the latest full plan/check exchange, with its paired tool result.
        # Earlier raw attempts remain in the durable diagnostic history.
        lane = (
            "plan"
            if observation.name == "update_delivery_plan"
            else f"check:{observation.data.get('check_id')}"
            if observation.name in {"run_check", "complete_work_item"}
            else ""
        )
        if lane and lane in seen:
            continue
        if lane:
            seen.add(lane)
        payload = _compact(observation.model_dump(mode="json"))
        if observation.name == "update_delivery_plan":
            # A complete pending graph is needed to repair coverage and edges.
            # Do not silently truncate it to the generic historical-list limit.
            payload["arguments"] = observation.arguments
        # Native assistant calls already carry arguments; do not duplicate the
        # full plan/source in the tool result sent back to the model.
        size = len(json.dumps(payload, ensure_ascii=False))
        if chars + size > 100_000:
            break
        retained.append(ToolExecutionResult.model_validate(payload))
        chars += size
    for observation in reversed(retained):
        call = ToolCall(
            id=observation.tool_call_id, name=observation.name, arguments=observation.arguments
        )
        append_tool_batch_exchange(
            history,
            result=ChatWithToolsResult(tool_calls=[call]),
            calls=[call],
            observations=[observation.model_copy(update={"arguments": {}})],
        )
    context = dict(engineering_context)
    repair = context.get("repair_context")
    if isinstance(repair, dict):
        # Current structured evidence is sufficient; raw check output remains durable.
        context["repair_context"] = {k: v for k, v in repair.items() if k != "failed_check"}
    history.append(
        {
            "role": "user",
            "content": json.dumps(
                {
                    "current_work_item": work_item.model_dump(mode="json"),
                    "engineering_context": context,
                },
                ensure_ascii=False,
            ),
        }
    )
    return history


def decide_next_action(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    engineering_context: dict[str, Any],
    observations: list[ToolExecutionResult],
    system_design: SystemDesign | None,
    tools: list[ToolDefinition] | None = None,
) -> ChatWithToolsResult:
    available = CODE_ENGINEER_TOOLS if tools is None else tools
    result = chat_with_tools(
        messages=build_code_engineer_messages(
            spec=spec,
            work_item=work_item,
            engineering_context=engineering_context,
            observations=observations,
            system_design=system_design,
        ),
        tools=available,
        temperature=0.1,
        max_tokens=16384,
    )
    if not result.tool_calls:
        raise ModelProtocolException("Code Engineer 未调用工具推进当前工作单元")
    allowed = {tool.name for tool in available} & CODE_ENGINEER_PROFILE.allowed_tools
    for call in result.tool_calls:
        if call.name not in allowed:
            raise ModelProtocolException(
                f"Code Engineer 无权使用工具：{call.name}；当前可用：{sorted(allowed)}"
            )
    # Reads can share a model turn. Mutations must consume the previous result.
    if len(result.tool_calls) > 1 and (
        len(result.tool_calls) > 16
        or any(
            call.name not in {"list_files", "read_file", "search_code"}
            for call in result.tool_calls
        )
    ):
        raise ModelProtocolException("每轮最多批量调用 16 个只读工具；写入或状态变更请单独调用")
    return result
