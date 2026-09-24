from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException, NotFoundException
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.task import Task, TaskRecipient, TaskStatus
from app.models.task_result import TaskResult
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.services.app_spec import read_app_spec

APPROVAL_VERSION = "requirements_approval_v1"

ENGINEERING_TASK_KEY = "engineering_delivery"
ARCHITECTURE_TASK_KEY = "system_design"
QUALITY_TASK_KEY = "quality_validation"


@dataclass(frozen=True, slots=True)
class EngineeringSource:
    input_item: ConfigurationItem
    source_task: Task
    source_plan: Plan
    app_spec_item: ConfigurationItem
    app_spec_task: Task
    app_spec_plan: Plan
    app_spec: AppSpec
    system_design: SystemDesign | None

    @property
    def delivery_path(self) -> str:
        return "designed" if self.system_design is not None else "direct"


def load_approved_app_spec(
    db: Session, project_id: int, run_id: str, item_id: str, *, lock: bool = False
) -> tuple[ConfigurationItem, Task, Plan, AppSpec]:
    """内部只读校验；调用方先校验归属。工程交付只能承接已批准且没有疑问的正式需求。"""
    statement = (
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
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    row = db.execute(statement).one_or_none()
    if row is None:
        raise NotFoundException("需求成果不存在或不属于当前构建的已登记任务")
    item, task, plan = row
    if (
        (item.semantic_type, item.state) != ("app_spec", "usable")
        or (task.recipient, task.expected_output_type, task.status)
        != ("Product Manager", "app_spec", "succeeded")
        or plan.status != "succeeded"
    ):
        raise ConflictException("只能将已完成的可用需求交给工程交付")
    spec = read_app_spec(item)
    if spec.open_questions:
        raise ConflictException("需求还有待确认问题，不能创建工程交付任务")
    return item, task, plan, spec


def load_engineering_source(
    db: Session, project_id: int, run_id: str, item_id: str, *, lock: bool = False
) -> EngineeringSource:
    """Resolve one frozen Code Engineer input to its approved product intent.

    Leader may route a small approved app directly to Code Engineer. Larger work
    still arrives through an immutable ``system_design`` produced by Architect.
    """

    direct = db.scalar(
        select(ConfigurationItem).where(
            ConfigurationItem.item_id == item_id,
            ConfigurationItem.project_id == project_id,
            ConfigurationItem.producer_run_id == run_id,
        )
    )
    if direct is not None and direct.semantic_type == "app_spec":
        app_item, app_task, app_plan, spec = load_approved_app_spec(
            db, project_id, run_id, item_id, lock=lock
        )
        return EngineeringSource(
            input_item=app_item,
            source_task=app_task,
            source_plan=app_plan,
            app_spec_item=app_item,
            app_spec_task=app_task,
            app_spec_plan=app_plan,
            app_spec=spec,
            system_design=None,
        )

    statement = (
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
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    row = db.execute(statement).one_or_none()
    if row is None:
        raise NotFoundException("工程输入成果不存在或不属于当前构建")
    item, task, plan = row
    if (
        (item.semantic_type, item.state) != ("system_design", "usable")
        or (task.recipient, task.expected_output_type, task.status)
        != (TaskRecipient.ARCHITECT.value, "system_design", TaskStatus.SUCCEEDED.value)
        or plan.status != "succeeded"
        or len(item.upstream_item_ids) != 1
    ):
        raise ConflictException("只能把已完成的可用系统设计交给 Code Engineer")
    try:
        design = SystemDesign.model_validate(item.payload)
    except ValidationError as exc:
        raise ConflictException("系统设计正文不符合要求") from exc
    app_item, app_task, app_plan, spec = load_approved_app_spec(
        db, project_id, run_id, item.upstream_item_ids[0], lock=lock
    )
    return EngineeringSource(
        input_item=item,
        source_task=task,
        source_plan=plan,
        app_spec_item=app_item,
        app_spec_task=app_task,
        app_spec_plan=app_plan,
        app_spec=spec,
        system_design=design,
    )


def _is_engineering_delivery_task(task: Task, item_id: str) -> bool:
    return (
        task.task_key == ENGINEERING_TASK_KEY
        and task.recipient == TaskRecipient.CODE_ENGINEER.value
        and task.expected_output_type == "code"
        and task.status == TaskStatus.PENDING.value
        and task.input_configuration_item_ids == [item_id]
        and not task.depends_on_task_ids
    )


def _is_claimed_engineering_delivery_task(task: Task, item_id: str) -> bool:
    return (
        task.task_key == ENGINEERING_TASK_KEY
        and task.recipient == TaskRecipient.CODE_ENGINEER.value
        and task.expected_output_type == "code"
        and task.status == TaskStatus.RUNNING.value
        and task.input_configuration_item_ids == [item_id]
        and not task.depends_on_task_ids
    )


def find_architecture_task(
    db: Session, source_plan: Plan, item_id: str, *, lock: bool = False
) -> Task | None:
    """Find the exact Architect assignment derived from one approved app_spec."""

    statement = (
        select(Plan)
        .where(
            Plan.project_id == source_plan.project_id,
            Plan.build_run_id == source_plan.build_run_id,
            Plan.version > source_plan.version,
            Plan.cause_message_id == source_plan.cause_message_id,
        )
        .order_by(Plan.version.asc())
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    for plan in db.scalars(statement).all():
        tasks_query = select(Task).where(Task.plan_id == plan.plan_id)
        if lock:
            tasks_query = tasks_query.with_for_update().execution_options(populate_existing=True)
        tasks = list(db.scalars(tasks_query).all())
        if len(tasks) != 1:
            continue
        task = tasks[0]
        if (
            task.task_key == ARCHITECTURE_TASK_KEY
            and task.recipient == TaskRecipient.ARCHITECT.value
            and task.expected_output_type == "system_design"
            and task.input_configuration_item_ids == [item_id]
            and not task.depends_on_task_ids
            and task.status
            in {
                TaskStatus.PENDING.value,
                TaskStatus.RUNNING.value,
                TaskStatus.SUCCEEDED.value,
            }
        ):
            return task
    return None


def find_pending_engineering_task(
    db: Session, source_plan: Plan, item_id: str, *, lock: bool = False
) -> Task | None:
    """Find a replayable pending engineering delivery for this completed design.

    Only Code Engineer delivery tasks are considered.
    """
    statement = (
        select(Plan)
        .where(
            Plan.project_id == source_plan.project_id,
            Plan.build_run_id == source_plan.build_run_id,
            Plan.version > source_plan.version,
        )
        .order_by(Plan.version.asc())
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    plans = list(db.scalars(statement).all())
    engineering: Task | None = None
    for plan in plans:
        tasks_query = select(Task).where(Task.plan_id == plan.plan_id)
        if lock:
            tasks_query = tasks_query.with_for_update().execution_options(populate_existing=True)
        tasks = list(db.scalars(tasks_query).all())
        if plan.cause_message_id != source_plan.cause_message_id or len(tasks) != 1:
            if plan.status == "pending":
                raise ConflictException("已有其他后续计划，不能重复派工")
            continue
        task = tasks[0]
        if plan.status == "pending" and _is_engineering_delivery_task(task, item_id):
            engineering = task
            break
        if plan.status == "running" and _is_claimed_engineering_delivery_task(task, item_id):
            # Already claimed; create_engineering_delivery_task uses find_claimed separately.
            continue
        if plan.status == "pending":
            raise ConflictException("已有后续任务与本次工程交付不一致")
        if task.status not in (TaskStatus.CANCELLED.value, TaskStatus.FAILED.value):
            raise ConflictException("已有其他后续计划，不能重复派工")
    return engineering


def find_claimed_engineering_task(
    db: Session, source_plan: Plan, item_id: str, *, lock: bool = False
) -> Task | None:
    """Return the running engineering delivery for this approval, if already claimed."""
    statement = (
        select(Plan)
        .where(
            Plan.project_id == source_plan.project_id,
            Plan.build_run_id == source_plan.build_run_id,
            Plan.version > source_plan.version,
            Plan.status == "running",
            Plan.cause_message_id == source_plan.cause_message_id,
        )
        .order_by(Plan.version.asc())
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    for plan in db.scalars(statement).all():
        tasks_query = select(Task).where(Task.plan_id == plan.plan_id)
        if lock:
            tasks_query = tasks_query.with_for_update().execution_options(populate_existing=True)
        tasks = list(db.scalars(tasks_query).all())
        if len(tasks) != 1:
            continue
        task = tasks[0]
        if _is_claimed_engineering_delivery_task(task, item_id):
            return task
    return None


def prepare_requirements_followup(db: Session, source: Plan, item_id: str) -> int:
    """Reserve the next version while no downstream plan exists."""
    latest = db.scalar(
        select(Plan)
        .where(Plan.build_run_id == source.build_run_id, Plan.project_id == source.project_id)
        .order_by(Plan.version.desc())
        .limit(1)
        .with_for_update()
    )
    if latest is None:
        raise ConflictException("需求计划不存在")
    if latest.plan_id != source.plan_id:
        raise ConflictException("已有后续计划，请刷新后继续")
    return latest.version + 1
