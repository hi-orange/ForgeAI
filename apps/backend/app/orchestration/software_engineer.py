"""Engineering coding loop: frozen inputs → tools → workspace files. Does not publish."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypedDict, cast

from sqlalchemy.orm import Session, sessionmaker

from app.agents.software_engineer import decide_next_action
from app.core.exceptions import BusinessException, ConflictException
from app.core.settings import settings
from app.generation.delivery import WorkItem, plan_delivery, work_items_to_json
from app.generation.workspace import default_workspace_path, workspace_is_ready
from app.models.user import User
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.services.engineering.claim import read_frozen_input_snapshot
from app.services.engineering.handoff import load_approved_app_spec
from app.services.task_execution import latest_execution, renew_execution_lease, utc_now
from app.tools.registry import execute_tool_call

CHECKPOINT_KIND = "engineering_checkpoint"
CHECKPOINT_SCHEMA_VERSION = 1
MAX_OBSERVATIONS = 12
MAX_ACTIVITY = 80

_TOOL_LABELS = {
    "list_files": "List files",
    "read_file": "Read file",
    "search_code": "Search code",
    "apply_patch": "Write file",
    "complete_work_item": "Complete work item",
    "report_blocked": "Report blocked",
}


def _activity_entry(call: ToolCall, result: ToolExecutionResult) -> dict[str, Any]:
    args = call.arguments
    detail = ""
    if call.name in {"read_file", "apply_patch"}:
        detail = str(args.get("path") or "")
    elif call.name == "list_files":
        detail = str(args.get("path") or ".")
    elif call.name == "search_code":
        detail = str(args.get("query") or "")[:80]
    elif call.name == "complete_work_item":
        detail = str(args.get("work_item_id") or "")
    else:
        detail = (result.summary or "")[:80]
    return {
        "id": call.id,
        "name": call.name,
        "label": _TOOL_LABELS.get(call.name, call.name),
        "detail": detail[:200],
        "ok": result.ok,
    }


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


def _workspace_root(project_id: int, run_id: str):
    return default_workspace_path(settings.runtime_data_root, project_id, run_id)


def _load_checkpoint(snapshot: dict[str, Any]) -> dict[str, Any]:
    checkpoint = snapshot.get("checkpoint")
    if isinstance(checkpoint, dict) and checkpoint.get("kind") == CHECKPOINT_KIND:
        return checkpoint
    return {
        "kind": CHECKPOINT_KIND,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "work_items": [],
        "current_work_item_id": None,
        "model_turns": 0,
        "tool_calls": 0,
        "observations": [],
        "blocked_reason": None,
        "outcome": "running",
        "activity": [],
    }


def _save_snapshot(db: Session, execution_id: str, snapshot: dict[str, Any]) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.task_execution import TaskExecution

    execution = db.get(TaskExecution, execution_id)
    if execution is None or execution.status != "running" or execution.active_slot != 1:
        raise ConflictException("执行编号已失效或任务正在由其他执行处理")
    if execution.expires_at <= utc_now():
        raise ConflictException("工程执行租约已过期，请显式恢复")
    # JSON columns do not detect in-place dict mutation; persist a new object.
    execution.draft = json.loads(json.dumps(snapshot))
    flag_modified(execution, "draft")
    db.commit()


def _current_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in items:
        if item.get("status") == "pending":
            return item
    return None


class _EngineeringNodes:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def prepare(self, state: _WorkflowState) -> dict[str, object]:
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
            if not workspace_is_ready(state["project_id"], state["build_run_id"]):
                raise ConflictException("应用工作区尚未准备好")
            checkpoint = _load_checkpoint(snapshot)
            if checkpoint.get("outcome") == "blocked" and checkpoint.get("blocked_reason") in {
                "模型调用预算已用尽",
                "工具调用预算已用尽",
            }:
                checkpoint["outcome"] = "running"
                checkpoint["blocked_reason"] = None
            if not checkpoint.get("work_items"):
                checkpoint["work_items"] = work_items_to_json(plan_delivery(spec))
            current = _current_item(checkpoint["work_items"])
            checkpoint["current_work_item_id"] = current["id"] if current else None
            if current is None:
                checkpoint["outcome"] = "generated"
            elif not checkpoint.get("activity"):
                checkpoint["activity"] = [
                    {
                        "id": "start_coding",
                        "name": "start",
                        "label": "Start coding",
                        "detail": str(current.get("title") or current["id"])[:200],
                        "ok": True,
                    }
                ]
            snapshot["checkpoint"] = checkpoint
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)
            return {
                "outcome": checkpoint["outcome"],
                "current_work_item_id": checkpoint["current_work_item_id"],
                "model_turns": checkpoint.get("model_turns") or 0,
                "tool_calls": checkpoint.get("tool_calls") or 0,
            }

    def step(self, state: _WorkflowState) -> dict[str, object]:
        with self._session_factory() as db:
            execution = latest_execution(db, state["task_id"], lock=True)
            if execution is None or execution.execution_id != state["execution_id"]:
                raise ConflictException("工程执行不存在或已结束")
            snapshot = read_frozen_input_snapshot(execution)
            if snapshot is None:
                raise ConflictException("工程执行缺少冻结输入快照")
            checkpoint = _load_checkpoint(snapshot)
            if checkpoint.get("outcome") in {"generated", "blocked"}:
                return {
                    "outcome": checkpoint["outcome"],
                    "current_work_item_id": checkpoint.get("current_work_item_id"),
                    "model_turns": checkpoint.get("model_turns") or 0,
                    "tool_calls": checkpoint.get("tool_calls") or 0,
                }
            _, _, _, spec = load_approved_app_spec(
                db,
                state["project_id"],
                state["build_run_id"],
                str(snapshot["approved_item_id"]),
            )
            if not checkpoint.get("work_items"):
                raise ConflictException("工程循环尚未规划工作单元")
            current_raw = _current_item(checkpoint["work_items"])
            if current_raw is None:
                checkpoint["outcome"] = "generated"
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                return {"outcome": "generated"}
            work_item = WorkItem.model_validate(current_raw)
            observations = [
                ToolExecutionResult.model_validate(item)
                for item in checkpoint.get("observations") or []
            ]
            root = _workspace_root(state["project_id"], state["build_run_id"])

        with self._session_factory() as db:
            execution = latest_execution(db, state["task_id"], lock=True)
            if execution is None or execution.execution_id != state["execution_id"]:
                raise ConflictException("工程执行不存在或已结束")
            snapshot = read_frozen_input_snapshot(execution)
            assert snapshot is not None
            checkpoint = _load_checkpoint(snapshot)
            waiting_id = f"model_{int(checkpoint.get('model_turns') or 0) + 1}"
            activity = list(checkpoint.get("activity") or [])
            activity.append(
                {
                    "id": waiting_id,
                    "name": "model",
                    "label": "Call model",
                    "detail": work_item.title[:200],
                    "ok": True,
                }
            )
            checkpoint["activity"] = activity[-MAX_ACTIVITY:]
            checkpoint.pop("last_error", None)
            snapshot["checkpoint"] = checkpoint
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)

        try:
            decision = decide_next_action(spec=spec, work_item=work_item, observations=observations)
        except BusinessException as exc:
            with self._session_factory() as db:
                execution = latest_execution(db, state["task_id"], lock=True)
                if execution is None or execution.execution_id != state["execution_id"]:
                    raise ConflictException("工程执行不存在或已结束") from exc
                snapshot = read_frozen_input_snapshot(execution)
                assert snapshot is not None
                checkpoint = _load_checkpoint(snapshot)
                checkpoint["last_error"] = str(exc)[:500]
                activity = list(checkpoint.get("activity") or [])
                if activity and activity[-1].get("name") == "model":
                    activity[-1] = {**activity[-1], "ok": False, "detail": str(exc)[:200]}
                else:
                    activity.append(
                        {
                            "id": f"error_{len(activity) + 1}",
                            "name": "error",
                            "label": "Blocked",
                            "detail": str(exc)[:200],
                            "ok": False,
                        }
                    )
                checkpoint["activity"] = activity[-MAX_ACTIVITY:]
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                renew_execution_lease(db, state["task_id"], execution.execution_id)
            return {
                "outcome": "blocked",
                "current_work_item_id": checkpoint.get("current_work_item_id"),
                "model_turns": checkpoint.get("model_turns") or 0,
                "tool_calls": checkpoint.get("tool_calls") or 0,
            }
        with self._session_factory() as db:
            execution = latest_execution(db, state["task_id"], lock=True)
            if execution is None or execution.execution_id != state["execution_id"]:
                raise ConflictException("工程执行不存在或已结束")
            snapshot = read_frozen_input_snapshot(execution)
            assert snapshot is not None
            checkpoint = _load_checkpoint(snapshot)
            checkpoint["model_turns"] = int(checkpoint.get("model_turns") or 0) + 1
            checkpoint.pop("last_error", None)
            if not decision.tool_calls:
                checkpoint["outcome"] = "blocked"
                checkpoint["blocked_reason"] = "模型没有提出工具调用"
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                renew_execution_lease(db, state["task_id"], execution.execution_id)
                return {"outcome": "blocked", "model_turns": checkpoint["model_turns"]}

            call = decision.tool_calls[0]

            def complete_work_item(args: dict[str, Any]) -> ToolExecutionResult:
                work_item_id = str(args.get("work_item_id") or "")
                current = _current_item(checkpoint["work_items"])
                if current is None or current["id"] != work_item_id:
                    return ToolExecutionResult(
                        tool_call_id=call.id,
                        name="complete_work_item",
                        ok=False,
                        error_code="WRONG_WORK_ITEM",
                        summary="只能结束当前工作单元",
                    )
                current["status"] = "completed"
                current["summary"] = str(args.get("summary") or "")[:500]
                next_item = _current_item(checkpoint["work_items"])
                checkpoint["current_work_item_id"] = next_item["id"] if next_item else None
                checkpoint["observations"] = []
                if next_item is None:
                    checkpoint["outcome"] = "generated"
                return ToolExecutionResult(
                    tool_call_id=call.id,
                    name="complete_work_item",
                    ok=True,
                    summary=f"工作单元 {work_item_id} 已记录为写入完成",
                    data={"next_work_item_id": checkpoint["current_work_item_id"]},
                )

            def report_blocked(args: dict[str, Any]) -> ToolExecutionResult:
                reason = str(args.get("reason") or "需求无法满足")
                checkpoint["outcome"] = "blocked"
                checkpoint["blocked_reason"] = reason[:500]
                return ToolExecutionResult(
                    tool_call_id=call.id,
                    name="report_blocked",
                    ok=True,
                    summary=reason[:200],
                )

            result = execute_tool_call(
                call,
                workspace_root=root,
                complete_work_item=complete_work_item,
                report_blocked=report_blocked,
            )
            result = result.model_copy(update={"arguments": dict(call.arguments)})
            checkpoint["tool_calls"] = int(checkpoint.get("tool_calls") or 0) + 1
            observations = list(checkpoint.get("observations") or [])
            observations.append(result.model_dump(mode="json"))
            checkpoint["observations"] = observations[-MAX_OBSERVATIONS:]
            activity = list(checkpoint.get("activity") or [])
            activity.append(_activity_entry(call, result))
            checkpoint["activity"] = activity[-MAX_ACTIVITY:]
            snapshot["checkpoint"] = checkpoint
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)
            return {
                "outcome": checkpoint.get("outcome") or "running",
                "current_work_item_id": checkpoint.get("current_work_item_id"),
                "model_turns": checkpoint.get("model_turns") or 0,
                "tool_calls": checkpoint.get("tool_calls") or 0,
            }


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
    state.update(nodes.prepare(state))
    while state.get("outcome") not in {"generated", "blocked"}:
        state.update(nodes.step(state))
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
