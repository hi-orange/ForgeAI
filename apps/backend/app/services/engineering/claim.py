"""Claim a SoftwareEngineer delivery task and freeze its execution inputs."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.generation.template_registry import DEFAULT_TEMPLATE_VERSION, load_template_metadata
from app.models.build_run import ACTIVE_BUILD_RUN_STATUSES, BuildRun, BuildRunStage, BuildRunStatus
from app.models.configuration_item import ConfigurationItemType
from app.models.plan import Plan, PlanStatus
from app.models.project import Project
from app.models.task import Task, TaskRecipient, TaskStatus
from app.models.task_execution import TaskExecution
from app.models.user import User
from app.schemas.app_spec import AppSpec
from app.services.engineering.handoff import ENGINEERING_TASK_KEY, load_approved_app_spec
from app.services.task_execution import EXECUTION_LEASE, latest_execution, utc_now

INPUT_SNAPSHOT_KIND = "engineering_input_snapshot"
INPUT_SNAPSHOT_SCHEMA_VERSION = 1
TOOL_STRATEGY_VERSION = "engineering_tools_v1"

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
    template_version: str = DEFAULT_TEMPLATE_VERSION,
    base_revision_id: str | None = None,
    call_budget: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build the immutable input bag for one engineering execution."""
    meta = load_template_metadata(template_version)
    stack = meta.get("stack") if isinstance(meta.get("stack"), dict) else {}
    return {
        "kind": INPUT_SNAPSHOT_KIND,
        "schema_version": INPUT_SNAPSHOT_SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "task_id": task_id,
        "approved_item_id": approved_item_id,
        "approved_spec_digest": approved_spec_digest(spec),
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
    if draft.get("schema_version") != INPUT_SNAPSHOT_SCHEMA_VERSION:
        return None
    return draft


def claim_software_engineer_task(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    template_version: str = DEFAULT_TEMPLATE_VERSION,
) -> tuple[Task, TaskExecution]:
    """Claim a pending engineering delivery and freeze its inputs on a new execution.

    Refresh / retry of an already-claimed task returns the same frozen snapshot and
    does not create another execution or call the model.
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
        if run.status not in ACTIVE_BUILD_RUN_STATUSES or run.active_slot != 1:
            raise ConflictException("构建任务已结束，不能领取工程任务")
        if run.status == BuildRunStatus.RUNNING.value and run.stage not in (
            BuildRunStage.PM.value,
            BuildRunStage.DEVELOPER.value,
        ):
            raise ConflictException("构建已进入其他阶段，不能领取工程任务")

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

        if task.recipient != TaskRecipient.SOFTWARE_ENGINEER.value:
            raise BusinessException("该任务不是分配给 SoftwareEngineer 的")
        if task.task_key != ENGINEERING_TASK_KEY:
            raise BusinessException("只能领取工程交付任务")
        if task.expected_output_type != ConfigurationItemType.CODE.value:
            raise BusinessException("工程交付任务的预期成果必须是 code")
        if len(task.input_configuration_item_ids) != 1:
            raise ConflictException("工程交付任务没有固定唯一的需求输入")
        if task.depends_on_task_ids:
            raise ConflictException("工程交付任务不能带依赖链")

        approved_item_id = task.input_configuration_item_ids[0]
        _, _, _, spec = load_approved_app_spec(db, project_id, run_id, approved_item_id, lock=True)

        if task.status == TaskStatus.RUNNING.value and plan.status == PlanStatus.RUNNING.value:
            current = latest_execution(db, task_id, lock=True)
            if current is None or current.status != "running" or current.active_slot != 1:
                raise ConflictException("工程任务缺少有效执行，不能静默重领")
            if current.expires_at <= utc_now():
                raise ConflictException("工程执行租约已过期，请显式恢复")
            snapshot = read_frozen_input_snapshot(current)
            if snapshot is None:
                raise ConflictException("工程执行缺少冻结输入快照")
            if snapshot.get("approved_item_id") != approved_item_id:
                raise ConflictException("冻结输入与任务定义不一致")
            if snapshot.get("approved_spec_digest") != approved_spec_digest(spec):
                raise ConflictException("获批需求已被替换，不能沿用旧执行")
            db.commit()
            db.refresh(task)
            db.refresh(current)
            return task, current

        if task.status != TaskStatus.PENDING.value:
            raise ConflictException("任务已被领取或已结束，不能重复领取")
        if plan.status not in (PlanStatus.PENDING.value, PlanStatus.RUNNING.value):
            raise ConflictException("计划已结束，不能领取其中的任务")

        other_running_plan = db.scalar(
            select(Plan.plan_id)
            .where(
                Plan.build_run_id == run_id,
                Plan.plan_id != plan.plan_id,
                Plan.status == PlanStatus.RUNNING.value,
            )
            .limit(1)
            .with_for_update()
        )
        if other_running_plan is not None:
            raise ConflictException("该构建已有另一份计划正在执行，不能混用计划")

        reserved = db.connection().execute(
            update(BuildRun)
            .where(
                BuildRun.project_id == project_id,
                BuildRun.run_id == run_id,
                BuildRun.status == run.status,
                BuildRun.stage == run.stage,
                BuildRun.active_slot == 1,
            )
            .values(
                status=BuildRunStatus.RUNNING.value,
                stage=BuildRunStage.DEVELOPER.value,
            )
        )
        if reserved.rowcount != 1:
            raise ConflictException("构建状态已改变，请重新读取后再领取")
        db.expire(run)

        claimed = db.connection().execute(
            update(Task)
            .where(
                Task.task_id == task_id,
                Task.plan_id == plan.plan_id,
                Task.status == TaskStatus.PENDING.value,
            )
            .values(status=TaskStatus.RUNNING.value)
        )
        if claimed.rowcount != 1:
            raise ConflictException("任务已被其他请求领取或状态已改变")
        db.expire(task)
        plan.status = PlanStatus.RUNNING.value

        now = utc_now()
        if latest_execution(db, task_id, lock=True) is not None:
            raise ConflictException("工程任务已有执行记录，请使用恢复入口")

        snapshot = build_frozen_input_snapshot(
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            approved_item_id=approved_item_id,
            spec=spec,
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
