"""One engineering model decision over frozen intent and real tool evidence."""

from __future__ import annotations

import json
from typing import Any

from app.agents.prompts.code_engineer import (
    CODE_ENGINEER_SYSTEM_PROMPT,
    ENGINEERING_PROGRESS_GUIDANCE,
    PLANNING_REPAIR_GUIDANCE,
)
from app.agents.roles import get_role_profile
from app.agents.tool_protocol import append_tool_batch_exchange
from app.core.exceptions import ModelProtocolException
from app.core.llm import chat_with_tools
from app.core.settings import settings
from app.generation.delivery import WorkItem
from app.generation.engineering.work_memory import (
    MAX_EVIDENCE_CHARS,
    READ_TOOLS,
    visible_read_fragment,
)
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
MAX_NATIVE_READ_BATCHES = 2
MAX_HISTORY_CHECK_OUTPUT_CHARS = 1200


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
    guidance = engineering_context.get("progress_guidance")
    inspection_action_required = (
        isinstance(guidance, dict) and guidance.get("inspection_action_required") is True
    )
    protocol_retry = engineering_context.get("protocol_retry")
    read_actions_unavailable = inspection_action_required or isinstance(protocol_retry, dict)
    evidence_hashes = {
        entry["path"]: entry["content_hash"] for entry in memory.get("source_evidence", [])
    }
    evidence_hashes.update(memory.get("source_hashes", {}))
    stale_paths = set(memory.get("stale_evidence_paths", []))
    checks = {row["check_id"]: row for row in engineering_context.get("check_verification", [])}
    retained_reads: set[tuple[Any, ...]] = set()
    retained_read_batches: set[tuple[str, int | str]] = set()
    retained_batches: set[tuple[str, int | str]] = set()
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
        if read_actions_unavailable and observation.ok and observation.name in READ_TOOLS:
            # The current source excerpts remain in working_memory. Replaying
            # successful native read calls after reads were removed from this
            # turn's tool list primes the model to request an unavailable tool.
            continue
        batch_id: tuple[str, int | str] = (
            ("turn", observation.model_turn)
            if observation.model_turn is not None
            else ("call", observation.tool_call_id)
        )
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
            # Native results are immediate feedback, while working_memory
            # carries hash-checked source across decisions. Replaying every
            # distinct range in the rolling history rewards serial inspection
            # and can dominate the next prompt. Keep the newest two decisions,
            # including every result from a read batch as one unit.
            if (
                batch_id not in retained_read_batches
                and len(retained_read_batches) >= MAX_NATIVE_READ_BATCHES
            ):
                continue
            retained_read_batches.add(batch_id)
            retained_reads.add(identity)
            # Different ranges in the same decision remain distinct evidence.
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
        if observation.name in {"run_check", "complete_work_item"}:
            output = observation.data.get("output")
            if isinstance(output, str) and len(output) > MAX_HISTORY_CHECK_OUTPUT_CHARS:
                # The raw log stays in the checkpoint. Native history needs the
                # check identity and structured diagnostic, not the full log on
                # every subsequent model turn.
                omitted = len(output) - 1000
                payload["data"]["output"] = (
                    output[:300]
                    + f"\n[历史检查输出省略 {omitted} 字符；完整日志已记录]\n"
                    + output[-700:]
                )
                payload["data"]["output_total_chars"] = len(output)
                payload["truncated"] = True
        if observation.name == "read_file" and observation.ok:
            projected = visible_read_fragment(observation.data)
            # The paired assistant tool call carries arguments; its native
            # result omits them. Keep long paths out of the result body so the
            # 20k budget cannot collapse structured source into an excerpt.
            payload["summary"] = observation.summary[-1000:]
            payload["data"] = {key: value for key, value in projected.items() if key != "path"}
            if projected.get("content_truncated"):
                payload["truncated"] = True
                if projected.get("line_too_long"):
                    payload["summary"] = (
                        f"{payload['summary']}；单行超过上下文长度，按行重读无法显示剩余内容，"
                        "请搜索相关片段或局部替换"
                    )
                else:
                    payload["summary"] = (
                        f"{payload['summary']}；当前上下文显示至第 "
                        f"{projected.get('end_line')} 行，后续从第 "
                        f"{projected.get('next_start_line')} 行定向读取"
                    )
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
        if chars + size > 100_000 and batch_id not in retained_batches:
            break
        retained.append(ToolExecutionResult.model_validate(payload))
        retained_batches.add(batch_id)
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
    planning_repair = context.get("planning_repair")
    if isinstance(planning_repair, dict):
        context["planning_repair"] = {
            **planning_repair,
            "instruction": PLANNING_REPAIR_GUIDANCE,
        }
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
    if inspection_action_required:
        history.append(
            {
                "role": "user",
                "content": (
                    "当前工作项的连续只读决策窗口已用完。此前的读取调用仅是历史记录，"
                    "本轮不能调用 read_file、list_files 或 search_code。"
                    "根据现有源码证据，用当前可用工具实现或验证；若工作项过宽且"
                    "update_delivery_plan 可用，可有效拆分待做节点；若缺少安全修改必需的"
                    "具体源码，调用 report_blocked 并说明缺口。当前可用工具："
                    + json.dumps(context.get("available_tools", []), ensure_ascii=False)
                ),
            }
        )
    if isinstance(protocol_retry, dict):
        tool_name = protocol_retry.get("tool_name")
        attempted = f" {tool_name} " if isinstance(tool_name, str) else " "
        history.append(
            {
                "role": "user",
                "content": (
                    f"上一轮{attempted}工具参数不是合法 JSON，因此整批工具均未执行；"
                    "此前已取得的源码证据仍有效。请基于当前上下文重发一个参数合法、范围较小的"
                    "修改或验证动作；若内容过长，拆成局部编辑。本轮暂不提供 read_file、"
                    "list_files 或 search_code；若确实缺少安全修改所需的源码，调用"
                    " report_blocked 说明具体缺口。当前可用工具："
                    + json.dumps(context.get("available_tools", []), ensure_ascii=False)
                ),
            }
        )
    failed_check_repair = context.get("failed_check_repair")
    if isinstance(failed_check_repair, dict):
        history.append(
            {
                "role": "user",
                "content": (
                    "当前源码与失败检查的源码身份相同，重复运行不会产生新的修复证据；"
                    "本轮不提供 run_check 或 complete_work_item。请根据 active_diagnostics "
                    "修改造成失败的源码，或执行诊断要求的有效生成/重规划动作；"
                    "确实无法安全修复时用 report_blocked 说明具体阻碍。失败检查："
                    + json.dumps(failed_check_repair.get("check_ids", []), ensure_ascii=False)
                    + "；当前可用工具："
                    + json.dumps(context.get("available_tools", []), ensure_ascii=False)
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
        parallel_tool_calls=True,
    )
    if not result.tool_calls:
        raise ModelProtocolException("Code Engineer 未调用工具推进当前工作单元")
    allowed = {tool.name for tool in available} & CODE_ENGINEER_PROFILE.allowed_tools
    for call in result.tool_calls:
        if call.name not in allowed:
            raise ModelProtocolException(
                f"Code Engineer 无权使用工具：{call.name}；当前可用：{sorted(allowed)}",
                data={"rejected_tool_call": call.model_dump(mode="json")},
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
