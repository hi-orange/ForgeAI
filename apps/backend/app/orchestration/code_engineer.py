"""Durable engineering loop: evidence -> tool decision -> fenced action -> verification."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict, cast

from sqlalchemy.orm import Session, sessionmaker

from app.agents.code_engineer import (
    CODE_ENGINEER_TOOLS,
    decide_next_action,
)
from app.agents.tool_protocol import log_tool_result
from app.core.exceptions import (
    BusinessException,
    ConflictException,
    ModelProtocolException,
    ProviderException,
)
from app.core.settings import settings
from app.generation.delivery import (
    PlannedWorkItem,
    WorkItem,
    plan_quality_repair,
    planning_item,
    replace_delivery_plan,
    work_items_to_json,
)
from app.generation.engineering.actions import PreparedAction, commit_action, prepare_action
from app.generation.engineering.budget import call_budget
from app.generation.engineering.checkpoint import (
    append_activity,
    block_checkpoint,
    current_item,
    load_checkpoint,
    record_check_failure,
    remember_recovery,
    resume_blocked_checkpoint,
)
from app.generation.engineering.checks import (
    apply_check_outcome,
    select_completion_check,
    source_file_hashes,
)
from app.generation.engineering.context import (
    build_workspace_file_index,
    update_file_index,
)
from app.generation.engineering.diagnostics import (
    MAX_IDENTICAL_TOOL_FAILURES,
    acceptance_feedback,
    active_diagnostics,
    check_verification,
    invalidate_source_evidence,
    reconcile_protocol_diagnostic,
    record_feedback,
    resolve_protocol_diagnostic,
)
from app.generation.engineering.work_memory import (
    implementation_stalled,
    progress_guidance,
    record_progress,
    working_context,
)
from app.generation.engineering.work_memory import record_result as record_work_result
from app.generation.workspace import default_workspace_path, workspace_is_ready
from app.models.user import User
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.schemas.test_report import TestReport
from app.services.engineering.claim import read_frozen_input_snapshot
from app.services.engineering.handoff import load_approved_app_spec
from app.services.source_artifact import persist_source_artifact
from app.services.task_execution import latest_execution, renew_execution_lease, utc_now
from app.services.workflow_events import append_event
from app.tools import checks as check_tools
from app.tools.check_diagnostics import analyze_check_failure
from app.tools.code_engineer import MUTATING_TOOLS, rejected_tool, validate_tool_call

MAX_PLAN_HISTORY = 24

SessionFactory = Callable[[], Session]


class _WorkflowInputState(TypedDict):
    user_id: int
    project_id: int
    build_run_id: str
    task_id: str
    execution_id: str


class _WorkflowOutputState(TypedDict):
    project_id: int
    build_run_id: str
    task_id: str
    execution_id: str
    outcome: str
    current_work_item_id: str | None
    model_turns: int
    tool_calls: int


class _WorkflowState(_WorkflowInputState, total=False):
    outcome: str
    current_work_item_id: str | None
    model_turns: int
    tool_calls: int


class _WorkflowUpdate(TypedDict, total=False):
    outcome: str
    current_work_item_id: str | None
    model_turns: int
    tool_calls: int


def _workspace_root(project_id: int, run_id: str) -> Path:
    return default_workspace_path(settings.runtime_data_root, project_id, run_id)


def _save_snapshot(db: Session, execution_id: str, snapshot: dict[str, Any]) -> None:
    from app.models.task_execution import TaskExecution
    from app.services.task_execution import save_execution_draft

    execution = db.get(TaskExecution, execution_id)
    if execution is None:
        raise ConflictException("执行编号已失效")
    save_execution_draft(db, execution, snapshot)


def _locked_current(
    db: Session, state: _WorkflowState
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    execution = latest_execution(db, state["task_id"], lock=True)
    if (
        execution is None
        or execution.execution_id != state["execution_id"]
        or execution.status != "running"
        or execution.active_slot != 1
        or execution.expires_at <= utc_now()
    ):
        raise ConflictException("工程执行已暂停或被替换")
    snapshot = read_frozen_input_snapshot(execution)
    if snapshot is None:
        raise ConflictException("工程执行缺少冻结输入快照")
    return execution, snapshot, load_checkpoint(snapshot)


def _paused_outcome(state: _WorkflowState) -> _WorkflowUpdate:
    return {
        "outcome": "paused",
        "current_work_item_id": state.get("current_work_item_id"),
        "model_turns": int(state.get("model_turns") or 0),
        "tool_calls": int(state.get("tool_calls") or 0),
    }


class _EngineeringNodes:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _result(checkpoint: dict[str, Any]) -> _WorkflowUpdate:
        return {
            "outcome": str(checkpoint.get("outcome") or "running"),
            "current_work_item_id": checkpoint.get("current_work_item_id"),
            "model_turns": int(checkpoint.get("model_turns") or 0),
            "tool_calls": int(checkpoint.get("tool_calls") or 0),
        }

    def prepare(self, state: _WorkflowState) -> _WorkflowUpdate:
        with self._session_factory() as db:
            execution = latest_execution(db, state["task_id"], lock=True)
            if (
                execution is None
                or execution.execution_id != state["execution_id"]
                or execution.status != "running"
            ):
                raise ConflictException("工程执行不存在或已结束")
            snapshot = read_frozen_input_snapshot(execution)
            if snapshot is None:
                raise ConflictException("工程执行缺少冻结输入快照")
            _, _, _, spec = load_approved_app_spec(
                db,
                state["project_id"],
                state["build_run_id"],
                str(snapshot["approved_item_id"]),
                lock=True,
            )
            design_payload = snapshot.get("system_design")
            if snapshot.get("delivery_path") == "designed" and not isinstance(design_payload, dict):
                raise ConflictException("工程执行缺少冻结系统设计")
            if isinstance(design_payload, dict):
                SystemDesign.model_validate(design_payload)
            if not workspace_is_ready(state["project_id"], state["build_run_id"]):
                raise ConflictException("应用工作区尚未准备好")

            checkpoint = load_checkpoint(snapshot)
            if checkpoint.get("outcome") == "generated":
                return self._result(checkpoint)
            if checkpoint.get("blocked_reason_code") == "CHECK_ENVIRONMENT_UNAVAILABLE":
                remember_recovery(checkpoint)
                checkpoint["outcome"] = "running"
                checkpoint["blocked_reason"] = None
                checkpoint.pop("blocked_reason_code", None)
            resumed = execution.attempt > 1 and resume_blocked_checkpoint(checkpoint)
            if resumed:
                repair = checkpoint.get("repair_context")
                excerpt = str(repair.get("excerpt") or "") if isinstance(repair, dict) else ""
                detail = "保留已完成文件，从上次真实检查结果继续修复"
                if excerpt.strip():
                    detail = f"{detail}：{excerpt.strip()}"
                append_activity(
                    checkpoint,
                    name="resume",
                    label="Resume build",
                    detail=detail,
                    ok=True,
                )

            if checkpoint.get("outcome") == "blocked":
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                return self._result(checkpoint)

            root = _workspace_root(state["project_id"], state["build_run_id"])
            if "initial_source_files" not in checkpoint:
                checkpoint["initial_source_files"] = source_file_hashes(
                    check_tools.source_snapshot(root)[0]
                )
            if not isinstance(checkpoint.get("workspace_file_index"), dict):
                checkpoint["workspace_file_index"] = build_workspace_file_index(root)
            if checkpoint.get("check_environment_execution_id") != execution.execution_id:
                environment = record_feedback(checkpoint, check_tools.check_environment())
                append_activity(
                    checkpoint,
                    name="check_environment",
                    label="Check environment",
                    detail=environment.summary,
                    ok=environment.ok,
                )
                if not environment.ok:
                    block_checkpoint(
                        checkpoint,
                        environment.summary,
                        reason_code=environment.error_code,
                    )
                    snapshot["checkpoint"] = checkpoint
                    _save_snapshot(db, execution.execution_id, snapshot)
                    renew_execution_lease(db, state["task_id"], execution.execution_id)
                    return self._result(checkpoint)
                checkpoint["check_environment_execution_id"] = execution.execution_id

            report_payload = snapshot.get("quality_report")
            quality_report = (
                TestReport.model_validate(report_payload)
                if isinstance(report_payload, dict)
                else None
            )
            if quality_report is not None:
                checkpoint["diagnostic_state"].setdefault(
                    "acceptance",
                    {
                        "status": "open",
                        "diagnostic": acceptance_feedback(quality_report).model_dump(mode="json"),
                        "repeated_attempts": 0,
                    },
                )
            if quality_report is not None and not checkpoint.get("work_items"):
                checkpoint["work_items"] = work_items_to_json(
                    plan_quality_repair(spec, quality_report)
                )
            # Old feature lists remain evidence, not a runnable fallback plan.
            # Never overwrite an accepted engineering graph when resuming.
            checkpoint["needs_delivery_plan"] = (
                quality_report is None and checkpoint.get("delivery_plan_version") != 1
            )
            current = current_item(checkpoint["work_items"])
            checkpoint["current_work_item_id"] = (
                None if checkpoint["needs_delivery_plan"] else current["id"] if current else None
            )
            if not any(
                item.get("id") == "tool_executor_started"
                for item in checkpoint.get("activity") or []
                if isinstance(item, dict)
            ):
                append_activity(
                    checkpoint,
                    name="start",
                    label="Start coding",
                    detail=("工程师根据源码证据选择工具动作；平台负责受控写入、检查与恢复。"),
                    ok=True,
                )
                checkpoint["activity"][-1]["id"] = "tool_executor_started"

            snapshot["checkpoint"] = checkpoint
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)
            return self._result(checkpoint)

    def step(self, state: _WorkflowState) -> _WorkflowUpdate:
        loaded = self._load_step(state)
        if isinstance(loaded, dict):
            return cast(_WorkflowUpdate, loaded)
        snapshot, checkpoint, spec, system_design, work_item, root = loaded
        tools = [
            tool
            for tool in CODE_ENGINEER_TOOLS
            if tool.name != "generate_image" or settings.image_generation_enabled
        ]
        if checkpoint.get("needs_delivery_plan"):
            tools = [
                tool
                for tool in tools
                if tool.name
                in {
                    "list_files",
                    "read_file",
                    "search_code",
                    "update_delivery_plan",
                    "report_blocked",
                }
            ]
        elif snapshot.get("quality_report") is not None:
            tools = [tool for tool in tools if tool.name != "update_delivery_plan"]
        if checkpoint.get("needs_delivery_plan") and int(
            checkpoint.get("planning_turns") or 0
        ) >= call_budget(snapshot, "max_planning_turns"):
            tools = [
                tool for tool in tools if tool.name in {"update_delivery_plan", "report_blocked"}
            ]
        available = sorted(tool.name for tool in tools)
        reconcile_protocol_diagnostic(checkpoint, available)
        self._persist_waiting(
            state,
            name="decide_action",
            label="Choose engineering action",
            detail=work_item.title,
            reserve_model=True,
            available_tools=available,
        )
        try:
            decision = decide_next_action(
                spec=spec,
                system_design=system_design,
                work_item=work_item,
                tools=tools,
                engineering_context={
                    "available_tools": available,
                    "thinking_enabled": checkpoint.get(
                        "thinking_enabled", settings.code_engineer_thinking
                    ),
                    "model_responses": checkpoint.get("model_responses", []),
                    "needs_delivery_plan": checkpoint.get("needs_delivery_plan", False),
                    "planning_turns_remaining": max(
                        0,
                        call_budget(snapshot, "max_planning_turns")
                        - int(checkpoint.get("planning_turns") or 0),
                    ),
                    "working_memory": working_context(checkpoint, root),
                    "observed_files": checkpoint.get("observed_hashes", {}),
                    "no_progress_steps": checkpoint.get("no_progress_steps", 0),
                    "recovery_context": checkpoint.get("recovery_context"),
                    "delivery_plan": checkpoint["work_items"],
                    "stack": snapshot.get("stack"),
                    "workspace_file_index": checkpoint.get("workspace_file_index"),
                    "modified_files": checkpoint.get("modified_files", []),
                    "completed_work_items": [
                        item
                        for item in checkpoint["work_items"]
                        if item.get("status") == "completed"
                    ],
                    "repair_context": checkpoint.get("repair_context"),
                    "api_client_contract": checkpoint.get("api_client_contract"),
                    "pending_integration": checkpoint.get("pending_integration", True),
                    "active_diagnostics": active_diagnostics(checkpoint),
                    "check_verification": check_verification(checkpoint),
                    "progress_guidance": progress_guidance(checkpoint),
                    "quality_report": snapshot.get("quality_report"),
                    "acceptance_test_plan": snapshot.get("acceptance_test_plan"),
                    "acceptance_test_hash": snapshot.get("acceptance_test_hash"),
                    "smoke_contract": check_tools.load_smoke_contract(),
                    "interrupted_action": checkpoint.get("pending_tool"),
                    "remaining_model_turns": call_budget(snapshot, "max_model_turns")
                    - int(checkpoint.get("model_turns") or 0)
                    - 1,
                },
                observations=[
                    ToolExecutionResult.model_validate(row)
                    for row in checkpoint.get("observations", [])
                ],
            )
            allowed = {tool.name for tool in tools}
            if any(call.name not in allowed for call in decision.tool_calls):
                raise ModelProtocolException(f"当前阶段不开放该工具；当前可用：{sorted(allowed)}")
        except ModelProtocolException as exc:
            call = ToolCall(
                id=f"protocol_{checkpoint.get('model_turns', 0)}", name="model_response"
            )
            result = rejected_tool(call, exc)
            result = result.model_copy(
                update={
                    "data": {
                        **result.data,
                        "available_tools": available,
                        **({"protocol_metadata": exc.data} if isinstance(exc.data, dict) else {}),
                    }
                }
            )
            return self._record_action(state, call, PreparedAction(result), root)
        except ProviderException:
            # The durable worker owns provider backoff and retryability. Preserve
            # this checkpoint instead of converting a network failure into a product block.
            raise
        except BusinessException as exc:
            return self._persist_failure(state, str(exc))
        if decision.reasoning_content is not None:
            with self._session_factory() as db:
                execution, current_snapshot, current = _locked_current(db, state)
                current["model_responses"] = [
                    *(current.get("model_responses") or []),
                    {
                        "model_turn": current["model_turns"],
                        "result": decision.model_dump(mode="json"),
                    },
                ][-24:]
                current_snapshot["checkpoint"] = current
                _save_snapshot(db, execution.execution_id, current_snapshot)
        for call in decision.tool_calls:
            update = self._execute_call(state, snapshot, checkpoint, work_item, root, call)
            if update.get("outcome") != "running":
                return update
        return update

    def _execute_call(
        self,
        state: _WorkflowState,
        snapshot: dict[str, Any],
        checkpoint: dict[str, Any],
        work_item: WorkItem,
        root: Path,
        call: ToolCall,
    ) -> _WorkflowUpdate:
        try:
            call = validate_tool_call(call)
        except ValueError as exc:
            return self._record_action(state, call, PreparedAction(rejected_tool(call, exc)), root)
        with self._session_factory() as db:
            execution, current_snapshot, current = _locked_current(db, state)
            needed = 2 if call.name == "complete_work_item" else 1
            if int(current.get("tool_calls") or 0) + needed > call_budget(
                snapshot, "max_tool_calls"
            ):
                block_checkpoint(current, "工具调用预算已用尽", reason_code="TOOL_BUDGET_EXHAUSTED")
                current_snapshot["checkpoint"] = current
                _save_snapshot(db, execution.execution_id, current_snapshot)
                return self._result(current)
            resolve_protocol_diagnostic(current)
            current["tool_calls"] = int(current.get("tool_calls") or 0) + 1
            current["pending_tool"] = call.model_dump(mode="json")
            current_snapshot["checkpoint"] = current
            _save_snapshot(db, execution.execution_id, current_snapshot)
            checkpoint = current
        if checkpoint.get("needs_delivery_plan") and call.name in MUTATING_TOOLS | {
            "complete_work_item",
            "run_check",
        }:
            action = PreparedAction(
                rejected_tool(call, ValueError("先提交有效工程依赖计划，再执行源码任务"))
            )
        elif call.name == "update_delivery_plan":
            action = PreparedAction(
                ToolExecutionResult(
                    tool_call_id=call.id,
                    name=call.name,
                    ok=True,
                    summary="请求更新工程依赖计划",
                    arguments=call.arguments,
                )
            )
        elif call.name == "complete_work_item":
            if call.arguments["work_item_id"] != work_item.id:
                action = PreparedAction(rejected_tool(call, ValueError("只能完成当前工作单元")))
            else:
                return self._check_step(state, snapshot, checkpoint, work_item, root)
        elif call.name == "report_blocked":
            return self._persist_failure(state, call.arguments["reason"])
        elif call.name == "run_check":
            action = PreparedAction(
                self._run_check(
                    root,
                    checkpoint,
                    check_id=call.arguments["check_id"],
                    tool_call_id=call.id,
                ).model_copy(update={"arguments": call.arguments})
            )
        else:
            action = prepare_action(call, root, checkpoint.get("observed_hashes", {}))
        return self._record_action(state, call, action, root)

    def _record_action(
        self,
        state: _WorkflowState,
        call: ToolCall,
        action: PreparedAction,
        root: Path,
    ) -> _WorkflowUpdate:
        with self._session_factory() as db:
            execution, snapshot, current = _locked_current(db, state)
            result = commit_action(action, root, revision_id=execution.execution_id)
            result = result.model_copy(update={"model_turn": current["model_turns"]})
            progressed = False
            if call.name == "update_delivery_plan" and result.ok:
                try:
                    if snapshot.get("quality_report") is not None:
                        raise ConflictException(
                            "独立验收修复只执行报告范围，不能替换为全量产品计划"
                        )
                    _, _, _, spec = load_approved_app_spec(
                        db,
                        state["project_id"],
                        state["build_run_id"],
                        str(snapshot["approved_item_id"]),
                    )
                    revised = replace_delivery_plan(
                        spec,
                        [PlannedWorkItem.model_validate(row) for row in call.arguments["items"]],
                        current["work_items"],
                        SystemDesign.model_validate(snapshot["system_design"])
                        if isinstance(snapshot.get("system_design"), dict)
                        else None,
                    )
                    if revised == current["work_items"] and not current.get("needs_delivery_plan"):
                        raise ConflictException("计划没有变化，请执行已就绪任务")
                    current["work_items"] = revised
                    progressed = True
                    current["delivery_plan_version"] = 1
                    current["needs_delivery_plan"] = False
                    next_item = current_item(revised)
                    current["current_work_item_id"] = next_item["id"] if next_item else None
                    current.setdefault("delivery_plan_history", []).append(
                        {
                            "reason": call.arguments["reason"],
                            "items": revised,
                            "model_turn": current["model_turns"],
                        }
                    )
                    result = result.model_copy(
                        update={
                            "summary": "工程依赖计划已保存；按已满足的依赖调度，不按 PRD 顺序",
                            "data": {"current_work_item_id": current["current_work_item_id"]},
                        }
                    )
                except (ConflictException, ValueError) as exc:
                    result = rejected_tool(call, exc)
            if call.name == "run_check":
                result = self._remember_check(
                    current, str(call.arguments.get("check_id", "unknown")), result
                )
            else:
                result = record_feedback(current, result)
            result = result.model_copy(update={"model_turn": current["model_turns"]})
            current.pop("pending_tool", None)
            history = list(current.get("observations") or [])
            history.append(result.model_dump(mode="json"))
            current["observations"] = history[-24:]
            hashes = dict(current.get("observed_hashes") or {})
            if result.ok and call.name == "read_file":
                path = result.data.get("path")
                digest = result.data.get("content_hash")
                if isinstance(path, str) and isinstance(digest, str):
                    hashes[path] = digest
            changed = (
                result.data.get("files")
                if result.ok and (action.updates or action.deletions)
                else []
            )
            modified = list(current.get("modified_files") or [])
            for entry in changed or []:
                path = entry["path"]
                if entry.get("deleted"):
                    hashes.pop(path, None)
                    index = current.get("workspace_file_index", {})
                    index["paths"] = [p for p in index.get("paths", []) if p != path]
                    current.setdefault("source_evidence", {}).pop(path, None)
                    current.setdefault("work_memory", {}).get(
                        str(current.get("current_work_item_id") or ""), {}
                    ).get("changed_files", {}).pop(path, None)
                else:
                    hashes[path] = entry["content_hash"]
                    update_file_index(current, path)
                if path not in modified:
                    modified.append(path)
            current["modified_files"] = modified
            current["observed_hashes"] = hashes
            if changed:
                current["revision_id"] = execution.execution_id
                invalidate_source_evidence(current)
            if result.ok and call.name == "generate_api_client":
                current["api_client_contract"] = {
                    "backend_files": result.data["backend_files"],
                    "generated_files": result.data["generated_files"],
                    "runtime_version": result.data["runtime_version"],
                }
                # Backend-only changes can leave generated bytes identical. The
                # refreshed contract still invalidates a cached API_CLIENT_STALE.
                current["check_cache"] = {}
            record_progress(current, result, changed=bool(changed), plan_changed=progressed)
            record_work_result(current, result, updates=action.updates)
            log_tool_result("Code Engineer", int(current["model_turns"]), call, result)
            append_activity(
                current, name=call.name, label=call.name, detail=result.summary, ok=result.ok
            )
            if result.error_code == "CHECK_ENVIRONMENT_UNAVAILABLE":
                block_checkpoint(current, result.summary, reason_code=result.error_code)
            elif (
                not result.ok
                and int(result.data.get("correction", {}).get("repeated_attempts", 0))
                >= MAX_IDENTICAL_TOOL_FAILURES
            ):
                block_checkpoint(
                    current,
                    (
                        f"相同输入连续 {MAX_IDENTICAL_TOOL_FAILURES} 次返回相同错误，"
                        f"停止无效重试：{result.summary}"
                    ),
                    reason_code="REPEATED_CHECK_FAILURE"
                    if call.name == "run_check"
                    else "REPEATED_TOOL_FAILURE",
                )
            elif current.get("needs_delivery_plan") and current["no_progress_steps"] >= 12:
                block_checkpoint(
                    current,
                    "连续 12 个工具动作没有新证据、计划或源码进展，停止本次执行",
                    reason_code="NO_SOURCE_PROGRESS",
                )
            if current.get("outcome") == "running" and implementation_stalled(current):
                block_checkpoint(
                    current,
                    "已提供当前事实指导，仍未解除检查阻塞或取得有效进展；保留现场等待显式继续",
                    reason_code="NO_SOURCE_PROGRESS",
                )
            snapshot["checkpoint"] = current
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)
            return self._result(current)

    @staticmethod
    def _remember_check(
        checkpoint: dict[str, Any], check_id: str, result: ToolExecutionResult
    ) -> ToolExecutionResult:
        result = record_feedback(checkpoint, result, check_id=check_id)
        if result.error_code in {
            "CHECK_ENVIRONMENT_UNAVAILABLE",
            "CHECK_TIMEOUT",
            "SOURCE_CHANGED",
        }:
            return result
        if not isinstance(result.data.get("source_hash"), str):
            return result
        cache = dict(checkpoint.get("check_cache") or {})
        if result.ok:
            for covered in result.data.get("covered_checks", []):
                cache.pop(covered, None)
        cache[check_id] = result.model_dump(mode="json")
        checkpoint["check_cache"] = cache
        if not result.ok:
            record_check_failure(checkpoint, result, check_id=check_id)
        else:
            for covered in result.data.get("covered_checks", []):
                diagnostic = checkpoint.get("diagnostic_state", {}).get(f"check:{covered}")
                if isinstance(diagnostic, dict):
                    diagnostic.update(
                        status="resolved",
                        verification_status="current",
                        verified_by=check_id,
                        verification_source_hash=result.data["source_hash"],
                    )
            repair = checkpoint.get("repair_context")
            if isinstance(repair, dict) and repair.get("check_id") in {
                check_id,
                *result.data.get("covered_checks", []),
            }:
                checkpoint["repair_context"] = None
        return result

    @staticmethod
    def _run_check(
        root: Path, checkpoint: dict[str, Any], *, check_id: str, tool_call_id: str
    ) -> ToolExecutionResult:
        payload, source_hash = check_tools.source_snapshot(root)
        contract = checkpoint.get("api_client_contract")
        if isinstance(contract, dict):
            current_files = source_file_hashes(payload)
            backend_files = {p: h for p, h in current_files.items() if p.startswith("backend/")}
            generated_files = {
                p: h
                for p, h in current_files.items()
                if p == "frontend/openapi.json" or p.startswith("frontend/src/client/")
            }
            if (
                contract.get("backend_files") != backend_files
                or contract.get("generated_files") != generated_files
                or contract.get("runtime_version") != check_tools.CHECK_RUNTIME_VERSION
            ):
                return ToolExecutionResult(
                    tool_call_id=tool_call_id,
                    name="run_check",
                    ok=False,
                    error_code="API_CLIENT_STALE",
                    summary="接口契约已过期，请先 generate_api_client 更新后再检查",
                    data={
                        "source_hash": source_hash,
                        "check_id": check_id,
                        "runtime_version": check_tools.CHECK_RUNTIME_VERSION,
                    },
                )
        cached = (checkpoint.get("check_cache") or {}).get(check_id)
        if (
            isinstance(cached, dict)
            and cached.get("data", {}).get("source_hash") == source_hash
            and cached.get("data", {}).get("runtime_version") == check_tools.CHECK_RUNTIME_VERSION
        ):
            return ToolExecutionResult.model_validate(cached).model_copy(
                update={
                    "tool_call_id": tool_call_id,
                    "summary": f"复用同版本检查证据：{cached['summary']}",
                    "data": {
                        **cached["data"],
                        "cache_hit": True,
                        "elapsed_ms": 0,
                        "cached_elapsed_ms": cached["data"].get("elapsed_ms"),
                    },
                }
            )
        return check_tools.run_check(root, check_id=check_id, tool_call_id=tool_call_id)

    def _load_step(
        self, state: _WorkflowState
    ) -> (
        tuple[dict[str, Any], dict[str, Any], AppSpec, SystemDesign | None, WorkItem, Path]
        | _WorkflowUpdate
    ):
        with self._session_factory() as db:
            try:
                execution, snapshot, checkpoint = _locked_current(db, state)
            except ConflictException:
                return _paused_outcome(state)
            if checkpoint.get("outcome") in {"generated", "blocked"}:
                return self._result(checkpoint)
            if int(checkpoint.get("model_turns") or 0) >= call_budget(snapshot, "max_model_turns"):
                block_checkpoint(
                    checkpoint, "模型调用预算已用尽", reason_code="MODEL_BUDGET_EXHAUSTED"
                )
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                renew_execution_lease(db, state["task_id"], execution.execution_id)
                return self._result(checkpoint)
            if int(checkpoint.get("tool_calls") or 0) >= call_budget(snapshot, "max_tool_calls"):
                block_checkpoint(
                    checkpoint, "工具调用预算已用尽", reason_code="TOOL_BUDGET_EXHAUSTED"
                )
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                renew_execution_lease(db, state["task_id"], execution.execution_id)
                return self._result(checkpoint)
            _, _, _, spec = load_approved_app_spec(
                db,
                state["project_id"],
                state["build_run_id"],
                str(snapshot["approved_item_id"]),
            )
            design_payload = snapshot.get("system_design")
            system_design = (
                SystemDesign.model_validate(design_payload)
                if isinstance(design_payload, dict)
                else None
            )
            current = current_item(checkpoint.get("work_items") or [])
            if checkpoint.get("needs_delivery_plan"):
                current = planning_item(spec).model_dump(mode="json")
            if current is None:
                block_checkpoint(
                    checkpoint,
                    "没有可执行的工程依赖节点，不能宣告生成完成",
                    reason_code="INVALID_DELIVERY_PLAN",
                )
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                return self._result(checkpoint)
            return (
                snapshot,
                checkpoint,
                spec,
                system_design,
                WorkItem.model_validate(current),
                _workspace_root(state["project_id"], state["build_run_id"]),
            )

    def _persist_waiting(
        self,
        state: _WorkflowState,
        *,
        name: str,
        label: str,
        detail: str,
        reserve_model: bool = False,
        available_tools: list[str] | None = None,
    ) -> None:
        with self._session_factory() as db:
            execution, snapshot, checkpoint = _locked_current(db, state)
            if available_tools is not None:
                reconcile_protocol_diagnostic(checkpoint, available_tools)
            if reserve_model and progress_guidance(checkpoint) is not None:
                checkpoint.setdefault("progress_guidance_turn", int(checkpoint["model_turns"]) + 1)
            if reserve_model:
                checkpoint.setdefault("thinking_enabled", settings.code_engineer_thinking)
                checkpoint["model_turns"] = int(checkpoint.get("model_turns") or 0) + 1
                if checkpoint.get("needs_delivery_plan"):
                    checkpoint["planning_turns"] = int(checkpoint.get("planning_turns") or 0) + 1
            append_activity(checkpoint, name=name, label=label, detail=detail, ok=True)
            checkpoint.pop("last_error", None)
            snapshot["checkpoint"] = checkpoint
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)

    def _persist_failure(
        self,
        state: _WorkflowState,
        message: str,
    ) -> _WorkflowUpdate:
        with self._session_factory() as db:
            execution, snapshot, checkpoint = _locked_current(db, state)
            checkpoint["last_error"] = message[:500]
            checkpoint.pop("pending_tool", None)
            block_checkpoint(checkpoint, message)
            snapshot["checkpoint"] = checkpoint
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)
            return self._result(checkpoint)

    def _check_step(
        self,
        state: _WorkflowState,
        snapshot: dict[str, Any],
        checkpoint: dict[str, Any],
        work_item: WorkItem,
        root: Path,
    ) -> _WorkflowUpdate:
        pending_ids = [
            str(item.get("id"))
            for item in checkpoint.get("work_items") or []
            if isinstance(item, dict) and item.get("status") == "pending"
        ]
        is_final_work_item = pending_ids == [work_item.id]
        source_payload, source_hash = check_tools.source_snapshot(root)
        file_hashes = source_file_hashes(source_payload)
        check_id, reason = select_completion_check(
            checkpoint,
            work_item,
            file_hashes,
            final=is_final_work_item,
            quality_repair=isinstance(snapshot.get("quality_report"), dict),
            runtime_version=check_tools.CHECK_RUNTIME_VERSION,
        )
        self._persist_waiting(
            state,
            name=f"run_{check_id}_check",
            label=f"Run {check_id} checks",
            detail=f"验证“{work_item.title}”：{reason}",
        )
        result = self._run_check(
            root,
            checkpoint,
            check_id=check_id,
            tool_call_id=f"{check_id}_{work_item.id}",
        )
        preview_snapshot: bytes | None = None
        preview_source_hash: str | None = None
        if result.ok:
            preview_snapshot, preview_source_hash = check_tools.source_snapshot(root)
            if (
                result.data.get("source_hash") != preview_source_hash
                or preview_source_hash != source_hash
            ):
                result = result.model_copy(
                    update={
                        "ok": False,
                        "error_code": "SOURCE_CHANGED",
                        "summary": "检查与调度时的源码身份不一致，需要重新检查",
                    }
                )
                preview_snapshot = None
                preview_source_hash = None

        with self._session_factory() as db:
            execution, current_snapshot, current = _locked_current(db, state)
            result = self._remember_check(current, check_id, result)
            current["tool_calls"] = int(current.get("tool_calls") or 0) + 1
            if result.ok and check_id == "all":
                current["integration_baseline"] = {
                    "source_hash": source_hash,
                    "files": file_hashes,
                    "runtime_version": check_tools.CHECK_RUNTIME_VERSION,
                }
                current["pending_integration"] = False
            elif result.ok:
                current["pending_integration"] = True
            pending = current.pop("pending_tool", {})
            completion_summary = pending.get("arguments", {}).get("summary", work_item.title)
            history = list(current.get("plan_history") or [])
            history.append(
                {
                    "kind": current.get("active_plan_kind") or "implementation",
                    "plan": {"work_item_id": work_item.id, "summary": completion_summary},
                    "quick_check": result.model_dump(mode="json") if check_id == "quick" else None,
                    "final_check": result.model_dump(mode="json") if check_id == "all" else None,
                    "check_reason": reason,
                    "check": result.model_dump(mode="json"),
                }
            )
            current["plan_history"] = history[-MAX_PLAN_HISTORY:]
            failure = None if result.ok else analyze_check_failure(result)
            append_activity(
                current,
                name=f"{check_id}_check_result",
                label=f"{check_id} checks {'passed' if result.ok else 'failed'}",
                detail=result.summary
                if failure is None
                else f"{result.summary}：{failure.excerpt}",
                ok=result.ok,
            )

            if result.ok and preview_snapshot is not None and preview_source_hash is not None:
                artifact = persist_source_artifact(
                    db,
                    project_id=state["project_id"],
                    snapshot_bytes=preview_snapshot,
                    source_hash=preview_source_hash,
                )
                event = append_event(
                    db,
                    idempotency_key=(
                        f"preview-revision:{state['build_run_id']}:"
                        f"{work_item.id}:{preview_source_hash}"
                    ),
                    stream_key=f"preview:{state['build_run_id']}",
                    event_type="preview_revision_published",
                    project_id=state["project_id"],
                    build_run_id=state["build_run_id"],
                    task_id=state["task_id"],
                    execution_id=execution.execution_id,
                    payload={
                        "source_hash": preview_source_hash,
                        "source_artifact_id": artifact.artifact_id,
                        "work_item_id": work_item.id,
                        "work_item_title": work_item.title,
                        "check_id": check_id,
                        "check_runtime_version": check_tools.CHECK_RUNTIME_VERSION,
                    },
                )
                current["preview_revision"] = {
                    "revision": event.sequence,
                    "source_hash": preview_source_hash,
                    "source_artifact_id": artifact.artifact_id,
                    "work_item_id": work_item.id,
                }
                append_activity(
                    current,
                    name="preview_revision_published",
                    label="Preview revision ready",
                    detail=f"Revision {event.sequence} · {work_item.title}",
                    ok=True,
                )

            # Attribute verification evidence to the item being checked before advancing.
            record_work_result(current, result)
            record_progress(current, result)
            apply_check_outcome(
                current,
                work_item=work_item,
                summary=completion_summary,
                result=result,
                max_repair_rounds=call_budget(current_snapshot, "max_repair_rounds"),
                check_runtime_version=check_tools.CHECK_RUNTIME_VERSION,
            )
            observation = result.model_copy(
                update={
                    "tool_call_id": pending.get("id", result.tool_call_id),
                    "model_turn": current["model_turns"],
                    "name": "complete_work_item",
                    "arguments": pending.get("arguments", {}),
                }
            )
            log_tool_result(
                "Code Engineer",
                int(current["model_turns"]),
                ToolCall(
                    id=observation.tool_call_id,
                    name=observation.name,
                    arguments=observation.arguments,
                ),
                observation,
            )
            current["observations"] = [
                *(current.get("observations") or []),
                observation.model_dump(mode="json"),
            ][-24:]
            if result.ok:
                current["no_progress_steps"] = 0
                current["steps_without_delivery"] = 0
                current.pop("progress_guidance_turn", None)
            current_snapshot["checkpoint"] = current
            _save_snapshot(db, execution.execution_id, current_snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)
            return self._result(current)


def run_engineering_workflow(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    execution_id: str,
    *,
    session_factory: SessionFactory | None = None,
) -> _WorkflowOutputState:
    if session_factory is None:
        if db.new or db.dirty or db.deleted:
            raise BusinessException("请先保存调用方待提交的修改，再启动工程循环")
        db.rollback()
        session_factory = cast(
            SessionFactory,
            sessionmaker(
                bind=db.get_bind(),
                autocommit=False,
                autoflush=False,
                expire_on_commit=False,
            ),
        )
    nodes = _EngineeringNodes(session_factory)
    state: _WorkflowState = {
        "user_id": user.id,
        "project_id": project_id,
        "build_run_id": run_id,
        "task_id": task_id,
        "execution_id": execution_id,
    }
    from app.telemetry.agent import agent_telemetry_scope

    try:
        with agent_telemetry_scope(
            project_id=project_id,
            build_run_id=run_id,
            task_id=task_id,
            execution_id=execution_id,
            role="Code Engineer",
            bind=db.get_bind(),
        ):
            state.update(nodes.prepare(state))
            while state.get("outcome") not in {"generated", "blocked", "paused"}:
                state.update(nodes.step(state))
    except ConflictException:
        state["outcome"] = "paused"
    return {
        "project_id": project_id,
        "build_run_id": run_id,
        "task_id": task_id,
        "execution_id": execution_id,
        "outcome": str(state.get("outcome") or "running"),
        "current_work_item_id": state.get("current_work_item_id"),
        "model_turns": int(state.get("model_turns") or 0),
        "tool_calls": int(state.get("tool_calls") or 0),
    }
