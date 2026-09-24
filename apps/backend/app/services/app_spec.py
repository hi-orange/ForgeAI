"""Read, validate, and approve immutable AppSpec artifacts.

This module owns the product-intent artifact boundary. Workflow routing belongs to
``services.requirements``; model execution and task dispatch do not belong here.
"""

import hashlib
import json
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.models.configuration_item import ConfigurationItem, ConfigurationItemType
from app.models.plan import Plan, PlanStatus
from app.models.requirement_clarification import RequirementClarification
from app.models.task import Task, TaskRecipient, TaskStatus
from app.models.task_result import TaskResult
from app.models.user import User
from app.schemas.app_spec import (
    APP_SPEC_SCHEMA_VERSION,
    AcceptanceCriterion,
    AppSpec,
    RequirementItem,
)
from app.schemas.configuration_item import ConfigurationItemRegistration
from app.schemas.plan import PlanCreate
from app.schemas.project_message import ProjectMessageCreate
from app.schemas.requirements import RequirementsApproval, RequirementsApprovalSelection
from app.schemas.task import TaskCreate
from app.services import configuration_manager
from app.services import plan as plan_service
from app.services import task as task_service
from app.services.project_message import stage_user_project_message
from app.services.task_execution import lock_run


def read_app_spec(item: ConfigurationItem) -> AppSpec:
    """Read a supported persisted product-intent version without changing its identity."""

    if item.state != "usable" or item.semantic_type != "app_spec":
        raise ConflictException("需求成果已不可用")
    if item.schema_version not in (1, APP_SPEC_SCHEMA_VERSION):
        raise ConflictException("需求版本暂不支持")
    try:
        return AppSpec.model_validate(item.payload)
    except ValidationError as exc:
        raise ConflictException("需求正文不符合要求") from exc


def load_previous_app_spec(db: Session, task: Task, *, lock: bool = False) -> AppSpec | None:
    """Load only the exact AppSpec pinned to a valid clarification task."""

    if task.depends_on_task_ids or len(task.input_configuration_item_ids) > 1:
        raise BusinessException("需求任务只支持无任务依赖、最多一个原 app_spec")
    if not task.input_configuration_item_ids:
        return None
    statement = (
        select(ConfigurationItem, RequirementClarification, Plan)
        .join(
            RequirementClarification,
            RequirementClarification.configuration_item_id == ConfigurationItem.item_id,
        )
        .join(Plan, Plan.plan_id == RequirementClarification.followup_plan_id)
        .join(TaskResult, TaskResult.configuration_item_id == ConfigurationItem.item_id)
        .where(
            Plan.plan_id == task.plan_id,
            ConfigurationItem.item_id == task.input_configuration_item_ids[0],
            ConfigurationItem.project_id == Plan.project_id,
            ConfigurationItem.producer_run_id == Plan.build_run_id,
            RequirementClarification.answer_message_id == Plan.cause_message_id,
            RequirementClarification.task_id == TaskResult.task_id,
        )
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    row = db.execute(statement).one_or_none()
    if row is None:
        raise BusinessException("原需求与本次补充任务的关联不正确")
    return read_app_spec(row[0])


def _index_items(items: list[RequirementItem]) -> dict[str, RequirementItem]:
    return {item.id: item for item in items}


def _selected_by_kind(
    selected: list[RequirementsApprovalSelection], kind: str
) -> list[RequirementsApprovalSelection]:
    return [item for item in selected if item.kind == kind]


def _resolve_items(
    selected: list[RequirementsApprovalSelection],
    source_by_id: dict[str, RequirementItem],
    *,
    allow_new: bool,
) -> list[RequirementItem]:
    resolved: list[RequirementItem] = []
    for item in selected:
        if item.id in source_by_id or allow_new:
            resolved.append(RequirementItem(id=item.id, text=item.text))
        else:
            raise ConflictException(f"批准清单引用了未知条目：{item.id}")
    return resolved


def _build_acceptance(
    source: AppSpec,
    features: list[RequirementItem],
    selected: list[RequirementsApprovalSelection],
) -> list[AcceptanceCriterion]:
    originals = _index_items(source.features)
    replacements = {item.id: item.acceptance for item in selected if item.acceptance}
    unchanged_ids = {
        item.id
        for item in features
        if item.id in originals
        and item.text == originals[item.id].text
        and item.id not in replacements
    }
    kept = [
        AcceptanceCriterion(id=item.id, text=item.text, source_ids=list(item.source_ids))
        for item in source.acceptance_criteria
        if item.source_ids and set(item.source_ids) <= unchanged_ids
    ]
    covered = {source_id for item in kept for source_id in item.source_ids}
    for feature in features:
        if feature.id in covered:
            continue
        acceptance = replacements.get(feature.id)
        if not acceptance:
            raise ConflictException(f"请为功能「{feature.text}」填写与当前内容一致的验收条件")
        kept.append(
            AcceptanceCriterion(
                id=f"ac_{uuid4().hex[:12]}",
                text=acceptance,
                source_ids=[feature.id],
            )
        )
    return kept


def approve_requirements(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    item_id: str,
    payload: RequirementsApproval,
) -> str:
    """Atomically save the user's exact checklist as an immutable approved AppSpec."""

    # Imported lazily because engineering handoff validates AppSpec through this module.
    from app.services.engineering.handoff import APPROVAL_VERSION, prepare_requirements_followup

    payload = RequirementsApproval.model_validate(payload.model_dump())
    canonical = json.dumps(
        payload.model_dump(exclude_none=True), ensure_ascii=False, sort_keys=True
    )
    result_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    try:
        run = lock_run(db, user, project_id, run_id)
        if run.status != "running" or run.active_slot != 1:
            raise ConflictException("当前构建不能批准需求")
        if run.stage not in ("pm", "developer"):
            raise ConflictException("当前构建不能批准需求")
        row = db.execute(
            select(ConfigurationItem, Task, Plan)
            .join(TaskResult, TaskResult.configuration_item_id == ConfigurationItem.item_id)
            .join(Task, Task.task_id == TaskResult.task_id)
            .join(Plan, Plan.plan_id == Task.plan_id)
            .where(
                ConfigurationItem.item_id == item_id,
                ConfigurationItem.project_id == project_id,
                ConfigurationItem.producer_run_id == run_id,
                Plan.project_id == project_id,
                Plan.build_run_id == run_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            raise NotFoundException("需求计划不存在或不属于当前构建")
        item, source_task, source_plan = row
        if (item.semantic_type, item.state) != ("app_spec", "usable") or (
            source_task.recipient,
            source_task.status,
            source_plan.status,
        ) != ("Product Manager", "succeeded", "succeeded"):
            raise ConflictException("只能批准已整理完成的需求")
        source = read_app_spec(item)
        link = db.get(RequirementClarification, item_id)
        if link is not None and link.followup_plan_id is not None:
            approved_row = db.execute(
                select(TaskResult, Task, Plan)
                .join(Task, Task.task_id == TaskResult.task_id)
                .join(Plan, Plan.plan_id == Task.plan_id)
                .where(Task.plan_id == link.followup_plan_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one_or_none()
            if approved_row is None:
                raise ConflictException("计划已被修改或批准，请刷新后继续")
            approved, approved_task, approved_plan = approved_row
            if approved.prompt_version != APPROVAL_VERSION or approved.result_hash != result_hash:
                raise ConflictException("计划已被修改或批准，请刷新后继续")
            if approved_task.status == TaskStatus.SUCCEEDED.value and approved_plan.status in {
                PlanStatus.PENDING.value,
                PlanStatus.RUNNING.value,
            }:
                plan_service.stage_succeeded_if_tasks_complete(db, approved_plan)
            if approved_plan.status != PlanStatus.SUCCEEDED.value:
                raise ConflictException("批准计划尚未完整保存，请刷新后重试")
            approved_id = approved.configuration_item_id
            db.commit()
            return approved_id
        if run.stage != "pm":
            raise ConflictException("当前构建不能批准需求")
        next_version = prepare_requirements_followup(db, source_plan, item_id)
        previous_result = db.get(TaskResult, source_task.task_id)
        if previous_result is not None and previous_result.prompt_version == APPROVAL_VERSION:
            raise ConflictException("这份需求已经批准")

        selected_features = _selected_by_kind(payload.selected, "feature")
        if not selected_features:
            raise ConflictException("至少需要保留一项功能")
        source_features = _index_items(source.features)
        source_data = _index_items(source.data_requirements)
        source_interface = _index_items(source.interface_requirements)
        source_constraints = _index_items(source.constraints)
        known_ids = (
            set(source_features)
            | set(source_data)
            | set(source_interface)
            | set(source_constraints)
        )
        for entry in payload.selected:
            if entry.kind != "feature" and entry.acceptance is not None:
                raise ConflictException("验收条件只能关联功能条目")
            if entry.kind != "feature" and entry.id not in known_ids:
                raise ConflictException(f"批准清单引用了未知条目：{entry.id}")
            if entry.kind == "feature" and entry.id not in known_ids and not entry.id:
                raise ConflictException("新增功能缺少稳定标识")

        features = _resolve_items(selected_features, source_features, allow_new=True)
        data_requirements = _resolve_items(
            _selected_by_kind(payload.selected, "data"), source_data, allow_new=False
        )
        interface_requirements = _resolve_items(
            _selected_by_kind(payload.selected, "interface"), source_interface, allow_new=False
        )
        constraints = _resolve_items(
            _selected_by_kind(payload.selected, "constraint"), source_constraints, allow_new=True
        )
        acceptance_criteria = _build_acceptance(source, features, selected_features)
        spec = AppSpec(
            goal=payload.goal,
            target_users=source.target_users,
            features=features,
            data_requirements=data_requirements,
            interface_requirements=interface_requirements,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
            open_questions=[],
        )
        message = stage_user_project_message(
            db,
            user,
            project_id,
            ProjectMessageCreate(
                # Approval is a user action in the conversation, not a generated summary.
                # The immutable AppSpec already carries the exact approved goal and checklist.
                content="批准",
                client_message_id=payload.client_message_id,
            ),
        )
        plan = plan_service.stage_plan(
            db,
            user,
            project_id,
            run_id,
            PlanCreate(
                version=next_version,
                cause_message_id=message.id,
                tasks=[
                    TaskCreate(
                        task_key="requirements",
                        recipient=TaskRecipient.PRODUCT_MANAGER,
                        title="保存用户批准的需求计划",
                        instructions="以用户勾选、编辑及新增的清单为完整范围，保存批准版本。",
                        expected_output_type=ConfigurationItemType.APP_SPEC,
                        input_configuration_item_ids=[item_id],
                    )
                ],
            ),
        )
        task = db.scalar(select(Task).where(Task.plan_id == plan.plan_id))
        assert task is not None
        approved_item = configuration_manager.stage_configuration_item(
            db,
            project_id=project_id,
            producer_run_id=run_id,
            submission=ConfigurationItemRegistration(
                semantic_type=ConfigurationItemType.APP_SPEC,
                schema_version=APP_SPEC_SCHEMA_VERSION,
                payload=spec.model_dump(mode="json"),
                upstream_item_ids=[item_id],
            ),
        )
        if approved_item.state != "usable":
            raise ConflictException("批准版本未能保存，请刷新后重试")
        task_service.stage_succeeded(task, allow_pending=True)
        plan_service.stage_succeeded_if_tasks_complete(db, plan)
        db.add(
            TaskResult(
                task_id=task.task_id,
                configuration_item_id=approved_item.item_id,
                result_hash=result_hash,
                source_message_ids=[message.id],
                context_truncated=False,
                model="user",
                prompt_version=APPROVAL_VERSION,
            )
        )
        if link is None:
            link = RequirementClarification(
                configuration_item_id=item_id, task_id=source_task.task_id
            )
            db.add(link)
        link.answer_message_id = message.id
        link.followup_plan_id = plan.plan_id
        approved_id = approved_item.item_id
        db.commit()
        return approved_id
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("计划保存冲突，请使用相同请求重试") from exc
    except Exception:
        db.rollback()
        raise
