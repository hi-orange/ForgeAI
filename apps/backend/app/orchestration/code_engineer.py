"""Platform-driven engineering: file plan -> context -> writes -> checks -> repair."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict, cast

from sqlalchemy.orm import Session, sessionmaker

from app.agents.code_engineer import generate_file_content, plan_work_item_files
from app.core.exceptions import BusinessException, ConflictException
from app.core.settings import settings
from app.generation.delivery import (
    MAX_FILES_PER_WORK_ITEM,
    FileTask,
    ImplementationPlan,
    WorkItem,
    plan_delivery,
    plan_quality_repair,
    work_items_to_json,
)
from app.generation.workspace import default_workspace_path, workspace_is_ready
from app.models.user import User
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.schemas.test_report import TestReport
from app.services.engineering.claim import DEFAULT_CALL_BUDGET, read_frozen_input_snapshot
from app.services.engineering.handoff import load_approved_app_spec
from app.services.task_execution import latest_execution, renew_execution_lease, utc_now
from app.tools import checks as check_tools
from app.tools import files as file_tools
from app.tools import project_deps as project_deps_tools
from app.tools.paths import is_text_file, safe_path_under_root

CHECKPOINT_KIND = "engineering_checkpoint"
CHECKPOINT_SCHEMA_VERSION = 4
EXECUTION_MODE = "planned_files_v1"
MAX_CONTEXT_FILES = 160
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


def _call_budget(snapshot: dict[str, Any], name: str) -> int:
    configured = snapshot.get("call_budget")
    value = configured.get(name) if isinstance(configured, dict) else None
    if isinstance(value, int) and value > 0:
        return value
    return DEFAULT_CALL_BUDGET[name]


def _build_workspace_file_index(root: Path) -> dict[str, Any]:
    listing = file_tools.list_files(root, tool_call_id="context_file_index")
    raw_files = listing.data.get("files") if listing.ok else []
    files = raw_files if isinstance(raw_files, list) else []
    paths = [
        str(item.get("path"))
        for item in files[:MAX_CONTEXT_FILES]
        if isinstance(item, dict) and item.get("path")
    ]
    return {
        "paths": paths,
        "visible_file_count": len(files),
        "truncated": bool(listing.truncated or len(files) > MAX_CONTEXT_FILES),
    }


def _compact_observation_history(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compatibility helper for persisted controller checkpoints and older imports."""

    return items[-20:]


def _new_checkpoint() -> dict[str, Any]:
    return {
        "kind": CHECKPOINT_KIND,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "execution_mode": EXECUTION_MODE,
        "work_items": [],
        "current_work_item_id": None,
        "active_file_plan": None,
        "active_plan_kind": None,
        "deferred_file_plan": [],
        "plan_history": [],
        "repair_rounds": {},
        "repair_context": None,
        "model_turns": 0,
        "writer_model_calls": 0,
        "planner_model_calls": 0,
        "tool_calls": 0,
        "modified_files": [],
        "blocked_reason": None,
        "outcome": "running",
        "activity": [],
    }


def _load_checkpoint(snapshot: dict[str, Any]) -> dict[str, Any]:
    raw = snapshot.get("checkpoint")
    if not isinstance(raw, dict) or raw.get("kind") != CHECKPOINT_KIND:
        return _new_checkpoint()

    checkpoint = raw
    previous_mode = checkpoint.get("execution_mode")
    checkpoint["schema_version"] = CHECKPOINT_SCHEMA_VERSION
    for key, default in (
        ("work_items", []),
        ("current_work_item_id", None),
        ("active_file_plan", None),
        ("active_plan_kind", None),
        ("deferred_file_plan", []),
        ("plan_history", []),
        ("repair_rounds", {}),
        ("repair_context", None),
        ("model_turns", 0),
        ("writer_model_calls", 0),
        ("planner_model_calls", 0),
        ("tool_calls", 0),
        ("modified_files", []),
        ("activity", []),
    ):
        checkpoint.setdefault(key, default)

    if previous_mode != EXECUTION_MODE:
        # Controller v3 could spend an entire attempt selecting and re-reading files. Preserve
        # real files and completed items, but never replay that observation loop after upgrade.
        checkpoint.update(
            execution_mode=EXECUTION_MODE,
            observations=[],
            active_file_plan=None,
            active_plan_kind=None,
            repair_context=None,
            migrated_from_controller=True,
        )
        if checkpoint.get("outcome") != "generated":
            checkpoint.update(
                outcome="running",
                blocked_reason=None,
                model_turns=0,
                tool_calls=0,
            )
            checkpoint.pop("blocked_reason_code", None)
            checkpoint.pop("last_error", None)
    return checkpoint


def _resume_blocked_checkpoint(checkpoint: dict[str, Any]) -> bool:
    """Make an explicit recovery attempt runnable from its last real evidence."""

    if checkpoint.get("outcome") != "blocked":
        return False
    reason = str(checkpoint.get("blocked_reason") or "")
    checkpoint["outcome"] = "running"
    checkpoint["blocked_reason"] = None
    checkpoint.pop("blocked_reason_code", None)
    checkpoint.pop("last_error", None)

    if "自动修复轮次已用尽" in reason:
        current = _current_item(checkpoint.get("work_items") or [])
        history = checkpoint.get("plan_history")
        latest = history[-1] if isinstance(history, list) and history else None
        failed_check = latest.get("check") if isinstance(latest, dict) else None
        if current is not None and isinstance(failed_check, dict):
            rounds = checkpoint.get("repair_rounds")
            rounds = rounds if isinstance(rounds, dict) else {}
            rounds[str(current.get("id") or "")] = 1
            checkpoint["repair_rounds"] = rounds
            checkpoint["repair_context"] = {
                "round": 1,
                "failed_check": failed_check,
                "previous_plan": latest.get("plan"),
                "modified_files": list(checkpoint.get("modified_files") or []),
            }
            checkpoint["active_file_plan"] = None
            checkpoint["active_plan_kind"] = None
    return True


def _save_snapshot(db: Session, execution_id: str, snapshot: dict[str, Any]) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.task_execution import TaskExecution

    execution = db.get(TaskExecution, execution_id)
    if execution is None or execution.status != "running" or execution.active_slot != 1:
        raise ConflictException("执行编号已失效或任务正在由其他执行处理")
    if execution.expires_at <= utc_now():
        raise ConflictException("工程执行租约已过期，请显式恢复")
    execution.draft = json.loads(json.dumps(snapshot))
    flag_modified(execution, "draft")
    db.commit()


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
    return execution, snapshot, _load_checkpoint(snapshot)


def _current_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in items if item.get("status") == "pending"), None)


def _append_activity(
    checkpoint: dict[str, Any],
    *,
    name: str,
    label: str,
    detail: str,
    ok: bool,
) -> None:
    activity = list(checkpoint.get("activity") or [])
    current = _current_item(checkpoint.get("work_items") or [])
    activity.append(
        {
            "id": f"{name}_{len(activity) + 1}",
            "name": name,
            "label": label,
            "detail": detail[:200],
            "ok": ok,
            "work_item_id": str(current.get("id") or "") if current else "",
            "work_item_title": str(current.get("title") or "") if current else "",
        }
    )
    # Activity is the durable, refresh-safe run history. Trimming the head made the
    # user-visible step counter move backwards during long builds.
    checkpoint["activity"] = activity


def _block_checkpoint(
    checkpoint: dict[str, Any], reason: str, *, reason_code: str | None = None
) -> None:
    checkpoint["outcome"] = "blocked"
    checkpoint["blocked_reason"] = reason[:500]
    if reason_code:
        checkpoint["blocked_reason_code"] = reason_code
    _append_activity(checkpoint, name="error", label="Blocked", detail=reason, ok=False)


def _paused_outcome(state: _WorkflowState) -> _WorkflowUpdate:
    return {
        "outcome": "paused",
        "current_work_item_id": state.get("current_work_item_id"),
        "model_turns": int(state.get("model_turns") or 0),
        "tool_calls": int(state.get("tool_calls") or 0),
    }


def _normalize_plan(root: Path, plan: ImplementationPlan) -> ImplementationPlan:
    normalized: list[FileTask] = []
    seen: set[str] = set()
    seen_ids: set[str] = set()
    for task in plan.files:
        if task.id in seen_ids:
            raise BusinessException(f"实施计划包含重复任务 id：{task.id}")
        seen_ids.add(task.id)
        path = task.path.replace("\\", "/").strip()
        target = safe_path_under_root(root, path, allow_create=True)
        if not is_text_file(target if target.exists() else Path(path)):
            raise BusinessException(f"实施计划包含不可编辑文件：{path}")
        if path in seen:
            raise BusinessException(f"实施计划包含重复目标文件：{path}")
        seen.add(path)
        contexts: list[str] = []
        for raw_context in task.context_paths:
            context = raw_context.replace("\\", "/").strip()
            if not context or context == path or context in contexts:
                continue
            safe_path_under_root(root, context, allow_create=True)
            contexts.append(context)
        normalized.append(
            task.model_copy(update={"path": path, "context_paths": contexts, "status": "pending"})
        )
    return plan.model_copy(update={"files": normalized})


def _next_file(plan: dict[str, Any]) -> dict[str, Any] | None:
    files = plan.get("files")
    if not isinstance(files, list):
        return None
    return next(
        (item for item in files if isinstance(item, dict) and item.get("status") == "pending"),
        None,
    )


def _promote_deferred_batch(
    *,
    work_item: WorkItem,
    deferred: list[dict[str, Any]],
) -> tuple[ImplementationPlan, list[dict[str, Any]]]:
    """Take the next up-to-MAX slice from deferred overflow and leave the rest queued."""

    batch_raw = deferred[:MAX_FILES_PER_WORK_ITEM]
    remaining = deferred[MAX_FILES_PER_WORK_ITEM:]
    files: list[FileTask] = []
    for index, item in enumerate(batch_raw, start=1):
        files.append(
            FileTask.model_validate(
                {
                    **item,
                    "id": f"file_{index:02d}",
                    "status": "pending",
                    "content_hash": None,
                }
            )
        )
    plan = ImplementationPlan(
        work_item_id=work_item.id,
        summary=(
            f"续写：{work_item.title}（本批 {len(files)} 个文件"
            + (f"，仍有 {len(remaining)} 个待续写" if remaining else "")
            + "）"
        ),
        files=files,
    )
    return plan, remaining


def _platform_file_context(root: Path, task: FileTask) -> tuple[dict[str, Any], str | None, int]:
    """Read the target and direct contracts without spending controller turns."""

    requested = [task.path, *task.context_paths]
    files: list[dict[str, Any]] = []
    expected_hash: str | None = None
    read_count = 0
    for index, path in enumerate(dict.fromkeys(requested)):
        target = safe_path_under_root(root, path, allow_create=True)
        role = "target" if index == 0 else "dependency"
        if not target.exists():
            files.append({"path": path, "role": role, "exists": False})
            continue
        result = file_tools.read_workspace_source(
            root,
            path=path,
            tool_call_id=f"auto_context_{task.id}_{index + 1}",
        )
        read_count += 1
        if not result.ok:
            raise BusinessException(f"平台无法装配 {path} 的完整上下文：{result.summary}")
        content = result.data.get("content")
        content_hash = result.data.get("content_hash")
        if not isinstance(content, str) or not isinstance(content_hash, str):
            raise BusinessException(f"平台读取 {path} 后没有得到完整源码")
        files.append(
            {
                "path": path,
                "role": role,
                "exists": True,
                "content_hash": content_hash,
                "content": content,
                "line_count": result.data.get("line_count"),
            }
        )
        if index == 0:
            expected_hash = content_hash
    return {"files": files}, expected_hash, read_count


def _update_file_index(checkpoint: dict[str, Any], path: str) -> None:
    index = checkpoint.get("workspace_file_index")
    if not isinstance(index, dict):
        return
    paths = list(index.get("paths") or [])
    if path not in paths and len(paths) < MAX_CONTEXT_FILES:
        paths.append(path)
        index["paths"] = sorted(paths)
        index["visible_file_count"] = max(int(index.get("visible_file_count") or 0), len(paths))


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

            checkpoint = _load_checkpoint(snapshot)
            if checkpoint.get("outcome") == "generated":
                return self._result(checkpoint)
            if checkpoint.get("blocked_reason_code") == "CHECK_ENVIRONMENT_UNAVAILABLE":
                checkpoint["outcome"] = "running"
                checkpoint["blocked_reason"] = None
                checkpoint.pop("blocked_reason_code", None)
            if checkpoint.get("outcome") == "blocked" and checkpoint.get("blocked_reason") in {
                "模型调用预算已用尽",
                "工具调用预算已用尽",
            }:
                checkpoint["outcome"] = "running"
                checkpoint["blocked_reason"] = None
                checkpoint["model_turns"] = 0
                checkpoint["tool_calls"] = 0
            resumed = execution.attempt > 1 and _resume_blocked_checkpoint(checkpoint)
            if resumed:
                _append_activity(
                    checkpoint,
                    name="resume",
                    label="Resume build",
                    detail="保留已完成文件，从上次真实检查结果继续修复",
                    ok=True,
                )

            root = _workspace_root(state["project_id"], state["build_run_id"])
            if not isinstance(checkpoint.get("workspace_file_index"), dict):
                checkpoint["workspace_file_index"] = _build_workspace_file_index(root)
            if checkpoint.get("check_environment_execution_id") != execution.execution_id:
                environment = check_tools.check_environment()
                _append_activity(
                    checkpoint,
                    name="check_environment",
                    label="Check environment",
                    detail=environment.summary,
                    ok=environment.ok,
                )
                if not environment.ok:
                    _block_checkpoint(
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
            current_plan = work_items_to_json(
                plan_quality_repair(spec, quality_report)
                if quality_report is not None
                else plan_delivery(spec)
            )
            if not checkpoint.get("work_items"):
                checkpoint["work_items"] = current_plan
            else:
                # Delivery descriptions are deterministic derivatives of the frozen AppSpec.
                # Refresh pending items after platform upgrades while preserving completed work.
                planned_by_id = {str(item["id"]): item for item in current_plan}
                refreshed: list[dict[str, Any]] = []
                for saved in checkpoint["work_items"]:
                    if not isinstance(saved, dict) or saved.get("status") != "pending":
                        refreshed.append(saved)
                        continue
                    planned = planned_by_id.get(str(saved.get("id") or ""))
                    refreshed.append(dict(planned) if planned is not None else saved)
                checkpoint["work_items"] = refreshed
            current = _current_item(checkpoint["work_items"])
            checkpoint["current_work_item_id"] = current["id"] if current else None
            if current is None:
                checkpoint["outcome"] = "generated"
            if not any(
                item.get("id") == "planned_executor_started"
                for item in checkpoint.get("activity") or []
                if isinstance(item, dict)
            ):
                _append_activity(
                    checkpoint,
                    name="start",
                    label="Start coding",
                    detail=(
                        "已切换到文件级实施：先生成文件计划，再由平台装配上下文、"
                        "逐文件写入并自动检查。"
                    ),
                    ok=True,
                )
                checkpoint["activity"][-1]["id"] = "planned_executor_started"

            snapshot["checkpoint"] = checkpoint
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)
            return self._result(checkpoint)

    def step(self, state: _WorkflowState) -> _WorkflowUpdate:
        loaded = self._load_step(state)
        if isinstance(loaded, dict):
            return cast(_WorkflowUpdate, loaded)
        snapshot, checkpoint, spec, system_design, work_item, root = loaded
        if checkpoint.get("active_file_plan") is None:
            return self._plan_step(
                state, snapshot, checkpoint, spec, system_design, work_item, root
            )
        if _next_file(checkpoint["active_file_plan"]) is not None:
            return self._write_step(
                state, snapshot, checkpoint, spec, system_design, work_item, root
            )
        return self._check_step(state, snapshot, checkpoint, work_item, root)

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
            if int(checkpoint.get("model_turns") or 0) >= _call_budget(snapshot, "max_model_turns"):
                _block_checkpoint(checkpoint, "模型调用预算已用尽")
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                renew_execution_lease(db, state["task_id"], execution.execution_id)
                return self._result(checkpoint)
            if int(checkpoint.get("tool_calls") or 0) >= _call_budget(snapshot, "max_tool_calls"):
                _block_checkpoint(checkpoint, "工具调用预算已用尽")
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
            current = _current_item(checkpoint.get("work_items") or [])
            if current is None:
                checkpoint["outcome"] = "generated"
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
        self, state: _WorkflowState, *, name: str, label: str, detail: str
    ) -> None:
        with self._session_factory() as db:
            execution, snapshot, checkpoint = _locked_current(db, state)
            _append_activity(checkpoint, name=name, label=label, detail=detail, ok=True)
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
            _block_checkpoint(checkpoint, message)
            snapshot["checkpoint"] = checkpoint
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)
            return self._result(checkpoint)

    def _plan_step(
        self,
        state: _WorkflowState,
        snapshot: dict[str, Any],
        checkpoint: dict[str, Any],
        spec: AppSpec,
        system_design: SystemDesign | None,
        work_item: WorkItem,
        root: Path,
    ) -> _WorkflowUpdate:
        repair_context = checkpoint.get("repair_context")
        is_repair = isinstance(repair_context, dict)
        repair_round = repair_context.get("round") if isinstance(repair_context, dict) else None
        self._persist_waiting(
            state,
            name="plan_repair" if is_repair else "plan_files",
            label="Plan repair" if is_repair else "Plan files",
            detail=(f"根据检查错误规划第 {repair_round} 轮修复" if is_repair else work_item.title),
        )
        try:
            batch = plan_work_item_files(
                spec=spec,
                work_item=work_item,
                workspace_file_index=checkpoint["workspace_file_index"],
                system_design=system_design,
                repair_context=repair_context if is_repair else None,
            )
            plan = _normalize_plan(root, batch.plan)
            deferred = [item.model_dump(mode="json") for item in batch.deferred_files]
        except (BusinessException, ConflictException) as exc:
            return self._persist_failure(state, str(exc), model_kind="planner")

        with self._session_factory() as db:
            execution, current_snapshot, current = _locked_current(db, state)
            current["model_turns"] = int(current.get("model_turns") or 0) + 1
            current["planner_model_calls"] = int(current.get("planner_model_calls") or 0) + 1
            current["active_file_plan"] = plan.model_dump(mode="json")
            current["active_plan_kind"] = "repair" if is_repair else "implementation"
            if is_repair:
                existing = current.get("deferred_file_plan")
                existing_rows = existing if isinstance(existing, list) else []
                active_paths = {item.path for item in plan.files} | {
                    str(item.get("path") or "") for item in deferred if isinstance(item, dict)
                }
                kept = [
                    item
                    for item in existing_rows
                    if isinstance(item, dict) and str(item.get("path") or "") not in active_paths
                ]
                deferred = [*deferred, *kept]
            current["deferred_file_plan"] = deferred
            detail = f"{plan.summary}（{len(plan.files)} 个文件）"
            if deferred:
                detail = f"{detail}；另有 {len(deferred)} 个文件排队续写"
            _append_activity(
                current,
                name="plan_ready",
                label="File plan ready",
                detail=detail,
                ok=True,
            )
            current_snapshot["checkpoint"] = current
            _save_snapshot(db, execution.execution_id, current_snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)
            return self._result(current)

    def _write_step(
        self,
        state: _WorkflowState,
        snapshot: dict[str, Any],
        checkpoint: dict[str, Any],
        spec: AppSpec,
        system_design: SystemDesign | None,
        work_item: WorkItem,
        root: Path,
    ) -> _WorkflowUpdate:
        plan = ImplementationPlan.model_validate(checkpoint["active_file_plan"])
        raw_task = _next_file(checkpoint["active_file_plan"])
        assert raw_task is not None
        task = FileTask.model_validate(raw_task)
        maximum_context_reads = len(dict.fromkeys([task.path, *task.context_paths]))
        if int(checkpoint.get("tool_calls") or 0) + maximum_context_reads + 1 > _call_budget(
            snapshot, "max_tool_calls"
        ):
            return self._persist_failure(state, "工具调用预算已用尽")
        try:
            platform_context, expected_hash, read_count = _platform_file_context(root, task)
        except (BusinessException, ConflictException) as exc:
            return self._persist_failure(state, str(exc))

        self._persist_waiting(
            state,
            name="generate_file",
            label="Generate file",
            detail=f"{task.path}（平台已装配 {len(platform_context['files'])} 个文件上下文）",
        )
        engineering_context = {
            "template_version": snapshot.get("template_version"),
            "stack": snapshot.get("stack") or {},
            "workspace_file_index": checkpoint.get("workspace_file_index") or {},
            "files_modified_in_this_run": list(checkpoint.get("modified_files") or []),
            "implementation_plan": plan.model_dump(mode="json"),
            "platform_file_context": platform_context,
            "repair_context": checkpoint.get("repair_context"),
            "quality_report": snapshot.get("quality_report"),
        }
        try:
            content = generate_file_content(
                spec=spec,
                work_item=work_item,
                path=task.path,
                file_description=task.description,
                engineering_context=engineering_context,
                observations=[],
                system_design=system_design,
            )
        except BusinessException as exc:
            return self._persist_failure(
                state,
                str(exc),
                model_kind="writer",
                tool_calls=read_count,
            )

        with self._session_factory() as db:
            execution, current_snapshot, current = _locked_current(db, state)
            current_plan = ImplementationPlan.model_validate(current["active_file_plan"])
            current_task = next((item for item in current_plan.files if item.id == task.id), None)
            if current_task is None or current_task.status != "pending":
                raise ConflictException("文件计划已变化，请重新继续工程执行")
            written = file_tools.apply_patch(
                root,
                path=current_task.path,
                content=content,
                expected_hash=expected_hash,
                tool_call_id=f"write_{work_item.id}_{current_task.id}",
            )
            current["model_turns"] = int(current.get("model_turns") or 0) + 1
            current["writer_model_calls"] = int(current.get("writer_model_calls") or 0) + 1
            current["tool_calls"] = int(current.get("tool_calls") or 0) + read_count + 1
            if not written.ok:
                _block_checkpoint(current, written.summary, reason_code=written.error_code)
            else:
                current_task.status = "completed"
                value = written.data.get("content_hash")
                current_task.content_hash = str(value) if isinstance(value, str) else None
                current["active_file_plan"] = current_plan.model_dump(mode="json")
                modified = list(current.get("modified_files") or [])
                if current_task.path not in modified:
                    modified.append(current_task.path)
                current["modified_files"] = modified[-80:]
                _update_file_index(current, current_task.path)
                _append_activity(
                    current,
                    name="write_file",
                    label="File written",
                    detail=current_task.path,
                    ok=True,
                )
                sync = project_deps_tools.sync_manifest_dependencies(
                    root,
                    current_task.path,
                    tool_call_id=f"sync_deps_{work_item.id}_{current_task.id}",
                )
                if sync is not None:
                    current["tool_calls"] = int(current.get("tool_calls") or 0) + 1
                    _append_activity(
                        current,
                        name="install_project_dependency",
                        label="Install project deps",
                        detail=sync.summary,
                        ok=sync.ok,
                    )
                    # Host package manager missing is non-fatal; isolation checks still
                    # install from the generated manifest. Real install failures block.
                    if not sync.ok and sync.error_code == "DEPENDENCY_INSTALL_FAILED":
                        _block_checkpoint(current, sync.summary, reason_code=sync.error_code)
            current_snapshot["checkpoint"] = current
            _save_snapshot(db, execution.execution_id, current_snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)
            return self._result(current)

    def _check_step(
        self,
        state: _WorkflowState,
        snapshot: dict[str, Any],
        checkpoint: dict[str, Any],
        work_item: WorkItem,
        root: Path,
    ) -> _WorkflowUpdate:
        del snapshot, checkpoint
        self._persist_waiting(
            state,
            name="run_check",
            label="Run checks",
            detail=f"自动验证“{work_item.title}”的最新源码",
        )
        result = check_tools.run_check(root, check_id="all", tool_call_id=f"check_{work_item.id}")
        with self._session_factory() as db:
            execution, current_snapshot, current = _locked_current(db, state)
            current["tool_calls"] = int(current.get("tool_calls") or 0) + 1
            plan = ImplementationPlan.model_validate(current["active_file_plan"])
            history = list(current.get("plan_history") or [])
            history.append(
                {
                    "kind": current.get("active_plan_kind") or "implementation",
                    "plan": plan.model_dump(mode="json"),
                    "check": result.model_dump(mode="json"),
                }
            )
            current["plan_history"] = history[-MAX_PLAN_HISTORY:]
            output = str(result.data.get("output") or "")
            _append_activity(
                current,
                name="check_result",
                label="Checks passed" if result.ok else "Checks failed",
                detail=result.summary if result.ok else f"{result.summary}：{output[-140:]}",
                ok=result.ok,
            )
            if result.ok:
                current_raw = _current_item(current["work_items"])
                if current_raw is None or current_raw.get("id") != work_item.id:
                    raise ConflictException("当前工作单元已变化")
                deferred_raw = current.get("deferred_file_plan")
                deferred = deferred_raw if isinstance(deferred_raw, list) else []
                pending_deferred = [item for item in deferred if isinstance(item, dict)]
                if pending_deferred:
                    next_plan, remaining = _promote_deferred_batch(
                        work_item=work_item, deferred=pending_deferred
                    )
                    current["active_file_plan"] = next_plan.model_dump(mode="json")
                    current["active_plan_kind"] = "implementation"
                    current["deferred_file_plan"] = remaining
                    current["repair_context"] = None
                    _append_activity(
                        current,
                        name="plan_ready",
                        label="Continue file batch",
                        detail=next_plan.summary,
                        ok=True,
                    )
                else:
                    current_raw["status"] = "completed"
                    current_raw["summary"] = plan.summary[:500]
                    current["active_file_plan"] = None
                    current["active_plan_kind"] = None
                    current["deferred_file_plan"] = []
                    current["repair_context"] = None
                    next_item = _current_item(current["work_items"])
                    current["current_work_item_id"] = next_item["id"] if next_item else None
                    _append_activity(
                        current,
                        name="work_item_complete",
                        label="Work item complete",
                        detail=work_item.title,
                        ok=True,
                    )
                    if next_item is None:
                        source_hash = result.data.get("source_hash")
                        if not isinstance(source_hash, str):
                            raise ConflictException("完整检查没有返回源码 hash")
                        current["outcome"] = "generated"
                        current["completed_source_hash"] = source_hash
            elif result.error_code == "CHECK_ENVIRONMENT_UNAVAILABLE":
                _block_checkpoint(current, result.summary, reason_code=result.error_code)
            else:
                rounds = current.get("repair_rounds")
                rounds = rounds if isinstance(rounds, dict) else {}
                round_number = int(rounds.get(work_item.id) or 0) + 1
                rounds[work_item.id] = round_number
                current["repair_rounds"] = rounds
                if round_number > _call_budget(current_snapshot, "max_repair_rounds"):
                    _block_checkpoint(current, "自动修复轮次已用尽，完整检查仍未通过")
                else:
                    current["repair_context"] = {
                        "round": round_number,
                        "failed_check": result.model_dump(mode="json"),
                        "previous_plan": plan.model_dump(mode="json"),
                        "modified_files": list(current.get("modified_files") or []),
                    }
                    current["active_file_plan"] = None
                    current["active_plan_kind"] = None
                    # Keep deferred_file_plan so overflow still runs after repair succeeds.
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
    try:
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
