"""One engineering model decision over frozen intent and real tool evidence."""

from __future__ import annotations

import json
from typing import Any

from app.agents.prompts.code_engineer import (
    CODE_ENGINEER_SYSTEM_PROMPT,
    ENGINEERING_PROGRESS_GUIDANCE,
)
from app.agents.roles import get_role_profile
from app.agents.tool_protocol import append_tool_batch_exchange
from app.core.exceptions import ModelProtocolException
from app.core.llm import chat_with_tools
from app.core.settings import settings
from app.generation.delivery import WorkItem
from app.generation.engineering.work_memory import MAX_EVIDENCE_CHARS
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
    if isinstance(value, str) and len(value) > MAX_EVIDENCE_CHARS:
        return value[:MAX_EVIDENCE_CHARS] + "\n[历史内容已截断；需要完整证据时请定向重新读取]"
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
    evidence_hashes.update(memory.get("source_hashes", {}))
    stale_paths = set(memory.get("stale_evidence_paths", []))
    checks = {row["check_id"]: row for row in engineering_context.get("check_verification", [])}
    retained_reads: set[tuple[Any, ...]] = set()
    responses = [
        (row["model_turn"], ChatWithToolsResult.model_validate(row["result"]))
        for row in engineering_context.get("model_responses", [])
    ]
    response_by_call = {
        (turn, call.id): response for turn, response in responses for call in response.tool_calls
    }
    counted_responses: set[tuple[int | None, str]] = set()
    for observation in reversed(observations[-24:]):
        # Protocol refusals are projected through active_diagnostics, never as a
        # fictitious callable tool. Retired focus exchanges remain in the audit.
        if observation.name in {"model_response", "update_work_item_focus"}:
            continue
        if observation.name in {"run_check", "complete_work_item"} and not observation.ok:
            current_check = checks.get(observation.data.get("check_id"))
            if current_check and current_check["last_outcome"] == "passed":
                # Durable history keeps the failure; do not reintroduce a solved
                # defect when its successful verification has left the window.
                continue
        if observation.name == "read_file" and observation.ok:
            data = observation.data
            path = data.get("path")
            identity = tuple(
                data.get(k) for k in ("path", "content_hash", "start_line", "end_line")
            )
            if (
                path in stale_paths
                or identity in retained_reads
                or (path in evidence_hashes and data.get("content_hash") != evidence_hashes[path])
            ):
                continue
            retained_reads.add(identity)
            # Preserve the latest native exchange for EACH range. Memory does
            # not replace immediate feedback, and a different range is new evidence.
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
        response = response_by_call.get((observation.model_turn, observation.tool_call_id))
        if (
            response is not None
            and (observation.model_turn, response.tool_calls[0].id) not in counted_responses
        ):
            size += len(response.model_dump_json())
            counted_responses.add((observation.model_turn, response.tool_calls[0].id))
        if chars + size > 100_000:
            break
        retained.append(ToolExecutionResult.model_validate(payload))
        chars += size
    retained_by_id = {
        (observation.model_turn, observation.tool_call_id): observation for observation in retained
    }
    emitted_responses: set[tuple[int | None, str]] = set()
    for observation in reversed(retained):
        response = response_by_call.get((observation.model_turn, observation.tool_call_id))
        if response is not None:
            response_id = (observation.model_turn, response.tool_calls[0].id)
            if response_id in emitted_responses:
                continue
            emitted_responses.add(response_id)
            # Native thinking belongs to one complete assistant batch. Never
            # split it, truncate its reasoning, or replay calls without results.
            if all(
                (observation.model_turn, call.id) in retained_by_id for call in response.tool_calls
            ):
                append_tool_batch_exchange(
                    history,
                    result=response,
                    calls=response.tool_calls,
                    observations=[
                        retained_by_id[(observation.model_turn, call.id)].model_copy(
                            update={"arguments": {}}
                        )
                        for call in response.tool_calls
                    ],
                )
            continue
        call = ToolCall(
            id=observation.tool_call_id, name=observation.name, arguments=observation.arguments
        )
        append_tool_batch_exchange(
            history,
            result=ChatWithToolsResult(tool_calls=[call]),
            calls=[call],
            observations=[observation.model_copy(update={"arguments": {}})],
        )
    context = {k: v for k, v in engineering_context.items() if k != "model_responses"}
    guidance = context.get("progress_guidance")
    if isinstance(guidance, dict):
        context["progress_guidance"] = {**guidance, "instruction": ENGINEERING_PROGRESS_GUIDANCE}
    repair = context.get("repair_context")
    if isinstance(repair, dict):
        # Current structured evidence is sufficient; raw check output remains durable.
        current_check = checks.get(repair.get("check_id"))
        context["repair_context"] = (
            None
            if current_check and current_check["last_outcome"] == "passed"
            else {
                **{k: v for k, v in repair.items() if k != "failed_check"},
                "historical": bool(repair.get("requires_recheck")),
            }
        )
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
            engineering_context={
                **engineering_context,
                "available_tools": sorted(t.name for t in available),
            },
            observations=observations,
            system_design=system_design,
        ),
        tools=available,
        temperature=0.1,
        max_tokens=16384,
        thinking=bool(engineering_context.get("thinking_enabled", settings.code_engineer_thinking)),
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
