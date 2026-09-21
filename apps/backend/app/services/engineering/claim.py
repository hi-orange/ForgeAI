"""Claim a Code Engineer delivery task and freeze its execution inputs."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.generation.template_registry import DEFAULT_TEMPLATE_VERSION, load_template_metadata
from app.models.build_run import BuildRun, BuildRunStage
from app.models.configuration_item import ConfigurationItemType
from app.models.plan import Plan, PlanStatus
from app.models.project import Project
from app.models.task import Task, TaskRecipient, TaskStatus
from app.models.task_execution import TaskExecution
from app.models.user import User
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.services import build_run as build_run_service
from app.services import plan as plan_service
from app.services import task as task_service
from app.services.engineering.handoff import ENGINEERING_TASK_KEY, load_engineering_source
from app.services.task_execution import EXECUTION_LEASE, latest_execution, utc_now

INPUT_SNAPSHOT_KIND = "engineering_input_snapshot"
INPUT_SNAPSHOT_SCHEMA_VERSION = 2
SUPPORTED_INPUT_SNAPSHOT_SCHEMA_VERSIONS = frozenset({1, 2})
TOOL_STRATEGY_VERSION = "engineering_tools_v3"

DEFAULT_CALL_BUDGET: dict[str, int] = {
    "max_model_turns": 40,
    "max_tool_calls": 120,
    "max_repair_rounds": 8,
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def approved_spec_digest(spec: AppSpec) -> str:
    payload = json.dumps(spec.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return _sha256_text(payload)


def template_digest(template_version: str = DEFAULT_TEMPLATE_VERSION) -> str:
    meta = load_template_metadata(template_version)
    payload = json.dumps(meta, sort_keys=True, ensure_ascii=False)
    return _sha256_text(f"{template_version}:{payload}")


def build_frozen_input_snapshot(
    *,
    project_id: int,
    run_id: str,
    task_id: str,
    approved_item_id: str,
    spec: AppSpec,
    design_item_id: str | None,
    system_design: SystemDesign | None,
    template_version: str = DEFAULT_TEMPLATE_VERSION,
    base_revision_id: str | None = None,
    call_budget: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build the immutable input bag for one engineering execution."""
    meta = load_template_metadata(template_version)
    stack_raw = meta.get("stack")
    stack: dict[str, Any] = stack_raw if isinstance(stack_raw, dict) else {}
    return {
        "kind": INPUT_SNAPSHOT_KIND,
        "schema_version": INPUT_SNAPSHOT_SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "task_id": task_id,
        "approved_item_id": approved_item_id,
        "approved_spec_digest": approved_spec_digest(spec),
        "delivery_path": "designed" if system_design is not None else "direct",
        "design_item_id": design_item_id,
        "system_design": (
            system_design.model_dump(mode="json") if system_design is not None else None
        ),
        "base_revision_id": base_revision_id,
        "template_version": template_version,
        "template_digest": template_digest(template_version),
        "stack": {
            "frontend": stack.get("frontend"),
            "backend": stack.get("backend"),
            "database": stack.get("database"),
        },
        "tool_strategy_version": TOOL_STRATEGY_VERSION,
        "acceptance_requirements": [
            criterion.model_dump(mode="json") for criterion in spec.acceptance_criteria
        ],
        "call_budget": dict(call_budget or DEFAULT_CALL_BUDGET),
        "workspace_key": run_id,
    }


def read_frozen_input_snapshot(execution: TaskExecution) -> dict[str, Any] | None:
    draft = execution.draft
    if not isinstance(draft, dict):
        return None
    if draft.get("kind") != INPUT_SNAPSHOT_KIND:
        return None
    if draft.get("schema_version") not in SUPPORTED_INPUT_SNAPSHOT_SCHEMA_VERSIONS:
        return None
    return draft


def claim_code_engineer_task(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    template_version: str = DEFAULT_TEMPLATE_VERSION,
    recovery_execution_id: str | None = None,
) -> tuple[Task, TaskExecution]:
    """Claim a pending engineering delivery and freeze its inputs on a new execution.

    Refresh of an already-claimed running task returns the same frozen snapshot.
    Failed or expired executions require ``recovery_execution_id`` to start a new attempt
    that reuses the previous draft (including any checkpoint).
    """
    try:
        project = db.scalar(
            select(Project)
            .where(Project.id == project_id, Project.user_id == user.id)
            .with_for_update()
        )
        if project is None:
            raise NotFoundException("项目不存在")
        run = db.scalar(
            select(BuildRun)
            .where(BuildRun.project_id == project_id, BuildRun.run_id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise NotFoundException("构建任务不存在")
        build_run_service.require_active_for_stages(
            run,
            {BuildRunStage.PM, BuildRunStage.ARCHITECT, BuildRunStage.DEVELOPER},
        )
        result = db.execute(
            select(Task, Plan)
            .join(Plan, Task.plan_id == Plan.plan_id)
            .where(
                Plan.project_id == project_id,
                Plan.build_run_id == run_id,
                Task.task_id == task_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if result is None:
            raise NotFoundException("任务不存在或不属于指定构建")
        task, plan = result

        if task.recipient != TaskRecipient.CODE_ENGINEER.value:
            raise BusinessException("该任务不是分配给 Code Engineer 的")
        if task.task_key != ENGINEERING_TASK_KEY:
            raise BusinessException("只能领取工程交付任务")
        if task.expected_output_type != ConfigurationItemType.CODE.value:
            raise BusinessException("工程交付任务的预期成果必须是 code")
        if len(task.input_configuration_item_ids) != 1:
            raise ConflictException("工程交付任务没有固定唯一的需求输入")
        if task.depends_on_task_ids:
            raise ConflictException("工程交付任务不能带依赖链")

        input_item_id = task.input_configuration_item_ids[0]
        source = load_engineering_source(db, project_id, run_id, input_item_id, lock=True)
        approved_item_id = source.app_spec_item.item_id
        design_item_id = source.input_item.item_id if source.system_design is not None else None
        spec = source.app_spec

        if task.status == TaskStatus.RUNNING.value and plan.status == PlanStatus.RUNNING.value:
            current = latest_execution(db, task_id, lock=True)
            now = utc_now()
            if (
                current is not None
                and current.status == "running"
                and current.active_slot == 1
                and current.expires_at > now
                and recovery_execution_id is None
            ):
                snapshot = read_frozen_input_snapshot(current)
                if snapshot is None:
                    raise ConflictException("工程执行缺少冻结输入快照")
                if snapshot.get("approved_item_id") != approved_item_id:
                    raise ConflictException("冻结输入与任务定义不一致")
                if snapshot.get("approved_spec_digest") != approved_spec_digest(spec):
                    raise ConflictException("获批需求已被替换，不能沿用旧执行")
                if snapshot.get("design_item_id") != design_item_id:
                    raise ConflictException("冻结系统设计与任务定义不一致")
                db.commit()
                db.refresh(task)
                db.refresh(current)
                return task, current
            if current is None:
                raise ConflictException("工程任务缺少有效执行，不能静默重领")
            if recovery_execution_id is None:
                if current.status == "running" and current.expires_at <= now:
                    raise ConflictException("工程执行租约已过期，请显式恢复")
                raise ConflictException("工程任务缺少有效执行，不能静默重领")
            if current.execution_id != recovery_execution_id or current.status == "succeeded":
                raise ConflictException("请使用当前执行编号显式恢复任务")
            snapshot = read_frozen_input_snapshot(current)
            if snapshot is None:
                raise ConflictException("工程执行缺少冻结输入快照")
            if snapshot.get("approved_item_id") != approved_item_id:
                raise ConflictException("冻结输入与任务定义不一致")
            if snapshot.get("approved_spec_digest") != approved_spec_digest(spec):
                raise ConflictException("获批需求已被替换，不能沿用旧执行")
            if snapshot.get("design_item_id") != design_item_id:
                raise ConflictException("冻结系统设计与任务定义不一致")
            if current.status == "running":
                current.status, current.active_slot, current.finished_at = "superseded", None, now
                db.flush()
            execution = TaskExecution(
                execution_id=f"exec_{uuid4().hex}",
                task_id=task_id,
                attempt=current.attempt + 1,
                status="running",
                active_slot=1,
                started_at=now,
                expires_at=now + EXECUTION_LEASE,
                draft=current.draft,
            )
            db.add(execution)
            db.commit()
            db.refresh(task)
            db.refresh(execution)
            return task, execution

        if task.status != TaskStatus.PENDING.value:
            raise ConflictException("任务已被领取或已结束，不能重复领取")
        if plan.status not in (PlanStatus.PENDING.value, PlanStatus.RUNNING.value):
            raise ConflictException("计划已结束，不能领取其中的任务")

        build_run_service.stage_running(
            db,
            run,
            stage=BuildRunStage.DEVELOPER,
            allowed_running_stages={
                BuildRunStage.PM,
                BuildRunStage.ARCHITECT,
                BuildRunStage.DEVELOPER,
            },
        )
        plan_service.stage_running(db, plan)
        task_service.stage_running(db, task)

        now = utc_now()
        if latest_execution(db, task_id, lock=True) is not None:
            raise ConflictException("工程任务已有执行记录，请使用恢复入口")

        snapshot = build_frozen_input_snapshot(
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            approved_item_id=approved_item_id,
            spec=spec,
            design_item_id=design_item_id,
            system_design=source.system_design,
            template_version=template_version,
        )
        execution = TaskExecution(
            execution_id=f"exec_{uuid4().hex}",
            task_id=task_id,
            attempt=1,
            status="running",
            active_slot=1,
            started_at=now,
            expires_at=now + EXECUTION_LEASE,
            draft=snapshot,
        )
        db.add(execution)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("工程任务已被另一执行领取") from exc
    except Exception:
        db.rollback()
        raise

    db.refresh(task)
    db.refresh(execution)
    return task, execution
