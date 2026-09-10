from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException, NotFoundException
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.task import Task, TaskRecipient, TaskStatus
from app.models.task_result import TaskResult
from app.schemas.app_spec import AppSpec
from app.services.requirement_inputs import read_app_spec

APPROVAL_VERSION = "requirements_approval_v1"

ENGINEERING_TASK_KEY = "engineering_delivery"
LEGACY_DESIGN_TASK_KEY = "system_design"


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
        != ("ProductManager", "app_spec", "succeeded")
        or plan.status != "succeeded"
    ):
        raise ConflictException("只能将已完成的可用需求交给工程交付")
    spec = read_app_spec(item)
    if spec.open_questions:
        raise ConflictException("需求还有待确认问题，不能创建工程交付任务")
    return item, task, plan, spec


def load_design_source(
    db: Session, project_id: int, run_id: str, item_id: str, *, lock: bool = False
) -> tuple[ConfigurationItem, Task, Plan, AppSpec]:
    """兼容旧调用名；语义与 load_approved_app_spec 相同。"""
    return load_approved_app_spec(db, project_id, run_id, item_id, lock=lock)


def _is_engineering_delivery_task(task: Task, item_id: str) -> bool:
    return (
        task.task_key == ENGINEERING_TASK_KEY
        and task.recipient == TaskRecipient.SOFTWARE_ENGINEER.value
        and task.expected_output_type == "code"
        and task.status == TaskStatus.PENDING.value
        and task.input_configuration_item_ids == [item_id]
        and not task.depends_on_task_ids
    )


def _is_legacy_design_task(task: Task, item_id: str) -> bool:
    return (
        task.task_key == LEGACY_DESIGN_TASK_KEY
        and task.recipient == TaskRecipient.SOLUTION_ARCHITECT.value
        and task.expected_output_type == "system_design"
        and task.status == TaskStatus.PENDING.value
        and task.input_configuration_item_ids == [item_id]
        and not task.depends_on_task_ids
    )


def find_pending_engineering_task(
    db: Session, source_plan: Plan, item_id: str, *, lock: bool = False
) -> Task | None:
    """Find a replayable pending engineering delivery for this approved intent.

    Also recognizes unfinished legacy SolutionArchitect design tasks so callers can
    convert them without inventing a second delivery for the same approval.
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
    legacy: Task | None = None
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
        if plan.status == "pending" and _is_legacy_design_task(task, item_id):
            legacy = task
            continue
        if plan.status == "pending":
            raise ConflictException("已有后续任务与本次工程交付不一致")
        if task.status not in (TaskStatus.CANCELLED.value, TaskStatus.FAILED.value):
            raise ConflictException("已有其他后续计划，不能重复派工")
    return engineering or legacy


def find_pending_design_task(
    db: Session, source_plan: Plan, item_id: str, *, lock: bool = False
) -> Task | None:
    """兼容旧调用名。"""
    return find_pending_engineering_task(db, source_plan, item_id, lock=lock)


def cancel_legacy_design_task(db: Session, task: Task) -> None:
    """Cancel an unfinished SolutionArchitect handoff before creating engineering delivery."""
    if not (
        task.task_key == LEGACY_DESIGN_TASK_KEY
        and task.recipient == TaskRecipient.SOLUTION_ARCHITECT.value
        and task.status == TaskStatus.PENDING.value
    ):
        raise ConflictException("只能取消未开始的旧设计任务")
    plan = db.scalar(select(Plan).where(Plan.plan_id == task.plan_id).with_for_update())
    if plan is None or plan.status != "pending":
        raise ConflictException("旧设计计划状态不允许转换")
    task.status = TaskStatus.CANCELLED.value
    plan.status = "cancelled"


def prepare_requirements_followup(db: Session, source: Plan, item_id: str) -> int:
    """Reserve the next version after retiring only an exact pending legacy assignment.

    The caller holds the run lock and commits the replacement in the same transaction.
    """
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
        legacy = find_pending_engineering_task(db, source, item_id, lock=True)
        if (
            legacy is None
            or legacy.task_key != LEGACY_DESIGN_TASK_KEY
            or legacy.plan_id != latest.plan_id
        ):
            raise ConflictException("已有后续计划，请刷新后继续")
        cancel_legacy_design_task(db, legacy)
    return latest.version + 1
