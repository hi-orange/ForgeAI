"""Durable engineering loop: evidence -> tool decision -> fenced action -> verification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict, cast

from sqlalchemy.orm import Session, sessionmaker

from app.agents.code_engineer import (
    CODE_ENGINEER_TOOLS,
    decide_next_action,
)
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
    resume_blocked_checkpoint,
)
from app.generation.engineering.checks import apply_check_outcome
from app.generation.engineering.context import (
    build_workspace_file_index,
    update_file_index,
)
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
                checkpoint["outcome"] = "running"
                checkpoint["blocked_reason"] = None
                checkpoint.pop("blocked_reason_code", None)
            if (
                execution.attempt > 1
                and checkpoint.get("outcome") == "blocked"
                and checkpoint.get("blocked_reason")
                in {
                    "模型调用预算已用尽",
                    "工具调用预算已用尽",
                }
            ):
                checkpoint["outcome"] = "running"
                checkpoint["blocked_reason"] = None
                checkpoint["model_turns"] = 0
                checkpoint["tool_calls"] = 0
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

            root = _workspace_root(state["project_id"], state["build_run_id"])
            if not isinstance(checkpoint.get("workspace_file_index"), dict):
                checkpoint["workspace_file_index"] = build_workspace_file_index(root)
            if checkpoint.get("check_environment_execution_id") != execution.execution_id:
                environment = check_tools.check_environment()
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
        self._persist_waiting(
            state,
            name="decide_action",
            label="Choose engineering action",
            detail=work_item.title,
            reserve_model=True,
        )
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
        try:
            decision = decide_next_action(
                spec=spec,
                system_design=system_design,
                work_item=work_item,
                tools=tools,
                engineering_context={
                    "needs_delivery_plan": checkpoint.get("needs_delivery_plan", False),
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
        except ModelProtocolException as exc:
            call = ToolCall(
                id=f"protocol_{checkpoint.get('model_turns', 0)}", name="model_response"
            )
            return self._record_action(state, call, PreparedAction(rejected_tool(call, exc)), root)
        except ProviderException:
            # The durable worker owns provider backoff and retryability. Preserve
            # this checkpoint instead of converting a network failure into a product block.
            raise
        except BusinessException as exc:
            return self._persist_failure(state, str(exc))
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
            needed = 3 if call.name == "complete_work_item" else 1
            if int(current.get("tool_calls") or 0) + needed > call_budget(
                snapshot, "max_tool_calls"
            ):
                block_checkpoint(current, "工具调用预算已用尽")
                current_snapshot["checkpoint"] = current
                _save_snapshot(db, execution.execution_id, current_snapshot)
                return self._result(current)
            current["tool_calls"] = int(current.get("tool_calls") or 0) + 1
            current["pending_tool"] = call.model_dump(mode="json")
            current_snapshot["checkpoint"] = current
            _save_snapshot(db, execution.execution_id, current_snapshot)
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
                self._remember_check(current, call.arguments["check_id"], result)
            current.pop("pending_tool", None)
            history = list(current.get("observations") or [])
            history.append(result.model_dump(mode="json"))
            current["observations"] = history[-24:]
            hashes = dict(current.get("observed_hashes") or {})
            if result.ok and call.name in {"read_file", "list_files", "search_code"}:
                fingerprint = hashlib.sha256(
                    json.dumps(
                        [call.name, call.arguments, result.data],
                        sort_keys=True,
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
                seen = list(current.get("context_fingerprints") or [])
                progressed = fingerprint not in seen
                if progressed:
                    current["context_fingerprints"] = [*seen, fingerprint]
            if result.ok and call.name == "read_file":
                path = result.data.get("path")
                digest = result.data.get("content_hash")
                if isinstance(path, str) and isinstance(digest, str):
                    hashes[path] = digest
            changed = result.data.get("files") if result.ok and action.updates else []
            modified = list(current.get("modified_files") or [])
            for entry in changed or []:
                path = entry["path"]
                hashes[path] = entry["content_hash"]
                if path not in modified:
                    modified.append(path)
                update_file_index(current, path)
            current["modified_files"] = modified
            current["observed_hashes"] = hashes
            if changed:
                current["revision_id"] = execution.execution_id
            if changed or progressed:
                current["no_progress_steps"] = 0
            else:
                current["no_progress_steps"] = int(current.get("no_progress_steps") or 0) + 1
            append_activity(
                current, name=call.name, label=call.name, detail=result.summary, ok=result.ok
            )
            if result.error_code == "CHECK_ENVIRONMENT_UNAVAILABLE":
                block_checkpoint(current, result.summary, reason_code=result.error_code)
            elif current["no_progress_steps"] >= 12:
                block_checkpoint(
                    current,
                    "连续 12 个工具动作没有新证据、计划或源码进展，停止本次执行",
                    reason_code="NO_SOURCE_PROGRESS",
                )
            snapshot["checkpoint"] = current
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)
            return self._result(current)

    @staticmethod
    def _remember_check(
        checkpoint: dict[str, Any], check_id: str, result: ToolExecutionResult
    ) -> None:
        if result.error_code in {
            "CHECK_ENVIRONMENT_UNAVAILABLE",
            "CHECK_TIMEOUT",
            "SOURCE_CHANGED",
        }:
            return
        if not isinstance(result.data.get("source_hash"), str):
            return
        cache = dict(checkpoint.get("check_cache") or {})
        cache[check_id] = result.model_dump(mode="json")
        checkpoint["check_cache"] = cache

    @staticmethod
    def _run_check(
        root: Path, checkpoint: dict[str, Any], *, check_id: str, tool_call_id: str
    ) -> ToolExecutionResult:
        source_hash = check_tools.source_snapshot(root)[1]
        cached = (checkpoint.get("check_cache") or {}).get(check_id)
        if isinstance(cached, dict) and cached.get("data", {}).get("source_hash") == source_hash:
            return ToolExecutionResult.model_validate(cached).model_copy(
                update={
                    "tool_call_id": tool_call_id,
                    "summary": f"复用同版本检查证据：{cached['summary']}",
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
                block_checkpoint(checkpoint, "模型调用预算已用尽")
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                renew_execution_lease(db, state["task_id"], execution.execution_id)
                return self._result(checkpoint)
            if int(checkpoint.get("tool_calls") or 0) >= call_budget(snapshot, "max_tool_calls"):
                block_checkpoint(checkpoint, "工具调用预算已用尽")
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
    ) -> None:
        with self._session_factory() as db:
            execution, snapshot, checkpoint = _locked_current(db, state)
            if reserve_model:
                checkpoint["model_turns"] = int(checkpoint.get("model_turns") or 0) + 1
            append_activity(checkpoint, name=name, label=label, detail=detail, ok=True)
            checkpoint.pop("last_error", None)
            snapshot["checkpoint"] = checkpoint
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)

    def _persist_failure(
        self,
        state: _WorkflowState,
        message: str,
        *,
        model_kind: str | None = None,
        tool_calls: int = 0,
    ) -> _WorkflowUpdate:
        with self._session_factory() as db:
            execution, snapshot, checkpoint = _locked_current(db, state)
            if model_kind is not None:
                checkpoint["model_turns"] = int(checkpoint.get("model_turns") or 0) + 1
                counter = f"{model_kind}_model_calls"
                checkpoint[counter] = int(checkpoint.get(counter) or 0) + 1
            checkpoint["tool_calls"] = int(checkpoint.get("tool_calls") or 0) + tool_calls
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
        del snapshot
        pending_ids = [
            str(item.get("id"))
            for item in checkpoint.get("work_items") or []
            if isinstance(item, dict) and item.get("status") == "pending"
        ]
        is_final_work_item = pending_ids == [work_item.id]
        self._persist_waiting(
            state,
            name="run_quick_check",
            label="Run quick checks",
            detail=f"快速验证“{work_item.title}”的语法和类型契约",
        )
        quick_result = self._run_check(
            root,
            checkpoint,
            check_id="quick",
            tool_call_id=f"quick_{work_item.id}",
        )
        preview_snapshot: bytes | None = None
        preview_source_hash: str | None = None
        if quick_result.ok:
            preview_snapshot, preview_source_hash = check_tools.source_snapshot(root)
            checked_hash = quick_result.data.get("source_hash")
            if checked_hash != preview_source_hash:
                quick_result = quick_result.model_copy(
                    update={
                        "ok": False,
                        "error_code": "SOURCE_CHANGED",
                        "summary": "快速检查后源码发生变化，需要重新检查",
                    }
                )
                preview_snapshot = None
                preview_source_hash = None

        final_result: ToolExecutionResult | None = None
        result = quick_result
        if quick_result.ok and is_final_work_item:
            self._persist_waiting(
                state,
                name="run_final_check",
                label="Run final checks",
                detail="所有工作项已生成，执行完整构建、迁移和真实前后端冒烟",
            )
            final_result = self._run_check(
                root,
                checkpoint,
                check_id="all",
                tool_call_id=f"final_{work_item.id}",
            )
            if final_result.ok and (
                final_result.data.get("source_hash") != preview_source_hash
                or check_tools.source_snapshot(root)[1] != preview_source_hash
            ):
                final_result = final_result.model_copy(
                    update={
                        "ok": False,
                        "error_code": "SOURCE_CHANGED",
                        "summary": "完整检查与当前源码身份不一致，需要重新验证",
                    }
                )
            result = final_result

        with self._session_factory() as db:
            execution, current_snapshot, current = _locked_current(db, state)
            self._remember_check(current, "quick", quick_result)
            if final_result is not None:
                self._remember_check(current, "all", final_result)
            current["tool_calls"] = int(current.get("tool_calls") or 0) + (
                2 if final_result is not None else 1
            )
            pending = current.pop("pending_tool", {})
            completion_summary = pending.get("arguments", {}).get("summary", work_item.title)
            history = list(current.get("plan_history") or [])
            history.append(
                {
                    "kind": current.get("active_plan_kind") or "implementation",
                    "plan": {"work_item_id": work_item.id, "summary": completion_summary},
                    "quick_check": quick_result.model_dump(mode="json"),
                    "final_check": (
                        final_result.model_dump(mode="json") if final_result is not None else None
                    ),
                    "check": result.model_dump(mode="json"),
                }
            )
            current["plan_history"] = history[-MAX_PLAN_HISTORY:]
            quick_failure = None if quick_result.ok else analyze_check_failure(quick_result)
            quick_excerpt = "" if quick_failure is None else quick_failure.excerpt
            append_activity(
                current,
                name="quick_check_result",
                label="Quick checks passed" if quick_result.ok else "Quick checks failed",
                detail=(
                    quick_result.summary
                    if quick_result.ok
                    else f"{quick_result.summary}：{quick_excerpt}"
                ),
                ok=quick_result.ok,
            )

            if quick_result.ok and preview_snapshot is not None and preview_source_hash is not None:
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
                        "check_id": "quick",
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

            if final_result is not None:
                final_failure = None if final_result.ok else analyze_check_failure(final_result)
                final_excerpt = "" if final_failure is None else final_failure.excerpt
                append_activity(
                    current,
                    name="final_check_result",
                    label="Final checks passed" if final_result.ok else "Final checks failed",
                    detail=(
                        final_result.summary
                        if final_result.ok
                        else f"{final_result.summary}：{final_excerpt}"
                    ),
                    ok=final_result.ok,
                )

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
                    "name": "complete_work_item",
                    "arguments": pending.get("arguments", {}),
                }
            )
            current["observations"] = [
                *(current.get("observations") or []),
                observation.model_dump(mode="json"),
            ][-24:]
            if result.ok:
                current["no_progress_steps"] = 0
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
