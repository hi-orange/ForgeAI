"""Architect task lifecycle and system_design artifact publication."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.prompts.architect import ARCHITECT_PROMPT_VERSION
from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.core.settings import settings
from app.models.build_run import BuildRunStage
from app.models.configuration_item import ConfigurationItem, ConfigurationItemType
from app.models.plan import Plan, PlanStatus
from app.models.task import Task, TaskRecipient, TaskStatus
from app.models.task_execution import TaskExecution
from app.models.task_result import TaskResult
from app.models.user import User
from app.schemas.agent_action import ToolExecutionResult
from app.schemas.configuration_item import ConfigurationItemRegistration
from app.schemas.system_design import SystemDesign
from app.services import build_run as build_run_service
from app.services import configuration_manager, task_execution
from app.services import plan as plan_service
from app.services import task as task_service
from app.services.engineering.handoff import ARCHITECTURE_TASK_KEY, load_approved_app_spec

_ACTIVITY_LABELS = {
    "read_artifact": "读取已批准需求",
    "editor_read": "读取必要文件",
    "editor_write": "整理系统设计",
    "terminal_list": "查看项目结构",
    "terminal_run": "检查工程环境",
    "write_system_design": "提交系统设计",
}
MAX_ARCHITECT_ACTIVITIES = 30


def _load_task(
    db: Session,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    lock: bool = False,
) -> tuple[Task, Plan]:
    statement = (
        select(Task, Plan)
        .join(Plan, Task.plan_id == Plan.plan_id)
        .where(
            Plan.project_id == project_id,
            Plan.build_run_id == run_id,
            Task.task_id == task_id,
        )
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    row = db.execute(statement).one_or_none()
    if row is None:
        raise NotFoundException("Architect 任务不存在或不属于指定构建")
    return row[0], row[1]


def _validate_task(task: Task) -> str:
    if (
        task.task_key != ARCHITECTURE_TASK_KEY
        or task.recipient != TaskRecipient.ARCHITECT.value
        or task.expected_output_type != ConfigurationItemType.SYSTEM_DESIGN.value
        or len(task.input_configuration_item_ids) != 1
        or task.depends_on_task_ids
    ):
        raise BusinessException("Architect 任务定义不符合系统设计约定")
    return task.input_configuration_item_ids[0]


def claim_architect_task(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    recovery_execution_id: str | None = None,
) -> tuple[Task, TaskExecution]:
    """Claim an Architect task and create a fenced execution attempt."""

    try:
        run = task_execution.lock_run(db, user, project_id, run_id)
        task, plan = _load_task(db, project_id, run_id, task_id, lock=True)
        app_spec_item_id = _validate_task(task)
        load_approved_app_spec(db, project_id, run_id, app_spec_item_id, lock=True)
        build_run_service.require_active_for_stages(
            run,
            {BuildRunStage.PM, BuildRunStage.ARCHITECT},
        )
        if task.status == TaskStatus.PENDING.value:
            build_run_service.stage_running(
                db,
                run,
                stage=BuildRunStage.ARCHITECT,
                allowed_running_stages={BuildRunStage.PM, BuildRunStage.ARCHITECT},
            )
            plan_service.stage_running(db, plan)
            task_service.stage_running(db, task)
            db.commit()
            db.refresh(task)
        elif not (
            task.status == TaskStatus.RUNNING.value
            and plan.status == PlanStatus.RUNNING.value
            and run.stage == BuildRunStage.ARCHITECT.value
        ):
            raise ConflictException("Architect 任务已结束或状态不一致")
    except Exception:
        db.rollback()
        raise

    execution = task_execution.start_execution(
        db,
        user,
        project_id,
        run_id,
        task_id,
        recovery_execution_id=recovery_execution_id,
    )
    return task, execution


def record_architect_activity(
    db: Session,
    task_id: str,
    execution_id: str,
    observation: ToolExecutionResult,
) -> None:
    """Persist compact user-visible progress while preserving the execution fence."""

    execution = task_execution.require_execution(db, task_id, execution_id, lock=True)
    assert execution is not None
    draft = dict(execution.draft or {})
    checkpoint = draft.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("kind") != "architect_checkpoint":
        checkpoint = {"kind": "architect_checkpoint", "activity": []}
    activity = list(checkpoint.get("activity") or [])
    activity.append(
        {
            "id": observation.tool_call_id,
            "name": observation.name,
            "label": _ACTIVITY_LABELS.get(observation.name, observation.name),
            "detail": observation.summary,
            "ok": observation.ok,
        }
    )
    checkpoint["activity"] = activity[-MAX_ARCHITECT_ACTIVITIES:]
    draft["checkpoint"] = checkpoint
    execution.draft = json.loads(json.dumps(draft, ensure_ascii=False))
    task_execution.renew_execution_lease(db, task_id, execution_id)


def complete_architect_task(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    execution_id: str,
    design: SystemDesign,
) -> ConfigurationItem:
    """Publish one immutable system_design and complete its exact Architect task."""

    design = SystemDesign.model_validate(design.model_dump())
    payload = design.model_dump(mode="json")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    try:
        run = task_execution.lock_run(db, user, project_id, run_id)
        task, plan = _load_task(db, project_id, run_id, task_id, lock=True)
        app_spec_item_id = _validate_task(task)
        if run.stage != BuildRunStage.ARCHITECT.value:
            raise ConflictException("当前构建不在 Architect 阶段")
        saved = db.get(TaskResult, task_id)
        if saved is not None:
            if saved.result_hash != result_hash:
                raise ConflictException("Architect 任务已登记不同结果，不能覆盖")
            item = db.scalar(
                select(ConfigurationItem).where(
                    ConfigurationItem.item_id == saved.configuration_item_id
                )
            )
            if item is None:
                raise ConflictException("Architect 任务产出关联异常")
            db.commit()
            return item
        if task.status != TaskStatus.RUNNING.value or plan.status != PlanStatus.RUNNING.value:
            raise ConflictException("Architect 任务没有提交资格")
        execution = task_execution.require_execution(db, task_id, execution_id, lock=True)
        assert execution is not None
        load_approved_app_spec(db, project_id, run_id, app_spec_item_id, lock=True)
        item = configuration_manager.stage_configuration_item(
            db,
            project_id=project_id,
            producer_run_id=run_id,
            submission=ConfigurationItemRegistration(
                semantic_type=ConfigurationItemType.SYSTEM_DESIGN,
                schema_version=1,
                payload=payload,
                upstream_item_ids=[app_spec_item_id],
            ),
        )
        db.add(
            TaskResult(
                task_id=task_id,
                configuration_item_id=item.item_id,
                result_hash=result_hash,
                source_message_ids=[plan.cause_message_id],
                context_truncated=False,
                model=settings.deepseek_model,
                prompt_version=ARCHITECT_PROMPT_VERSION,
            )
        )
        task_service.stage_succeeded(task)
        execution.status = "succeeded"
        execution.active_slot = None
        execution.finished_at = task_execution.utc_now()
        plan_service.stage_succeeded_if_tasks_complete(db, plan)
        db.commit()
        db.refresh(item)
        return item
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("系统设计成果保存冲突，请重试") from exc
    except Exception:
        db.rollback()
        raise
