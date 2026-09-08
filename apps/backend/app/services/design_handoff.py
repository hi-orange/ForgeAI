from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException, NotFoundException
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.task import Task
from app.models.task_result import TaskResult
from app.schemas.app_spec import APP_SPEC_SCHEMA_VERSION, AppSpec


def load_design_source(
    db: Session, project_id: int, run_id: str, item_id: str, *, lock: bool = False
) -> tuple[ConfigurationItem, Task, Plan, AppSpec]:
    """内部只读校验；调用方先校验归属。设计只能承接已完成且没有疑问的正式需求。"""
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
        (item.semantic_type, item.state, item.schema_version)
        != ("app_spec", "usable", APP_SPEC_SCHEMA_VERSION)
        or (task.recipient, task.expected_output_type, task.status)
        != ("ProductManager", "app_spec", "succeeded")
        or plan.status != "succeeded"
    ):
        raise ConflictException("只能将已完成的可用需求交给架构师")
    try:
        spec = AppSpec.model_validate(item.payload)
    except ValidationError as exc:
        raise ConflictException("需求正文不符合要求，不能创建设计任务") from exc
    if spec.open_questions:
        raise ConflictException("需求还有待确认问题，不能创建设计任务")
    return item, task, plan, spec


def find_pending_design_task(
    db: Session, source_plan: Plan, item_id: str, *, lock: bool = False
) -> Task | None:
    """准确下一版的设计交接；不把其他后续计划或已取消任务当作可重放的派工。"""
    statement = select(Plan).where(
        Plan.project_id == source_plan.project_id,
        Plan.build_run_id == source_plan.build_run_id,
        Plan.version == source_plan.version + 1,
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    plan = db.scalar(statement)
    if plan is None:
        return None
    tasks_query = select(Task).where(Task.plan_id == plan.plan_id)
    if lock:
        tasks_query = tasks_query.with_for_update().execution_options(populate_existing=True)
    tasks = list(db.scalars(tasks_query).all())
    if (
        plan.cause_message_id != source_plan.cause_message_id
        or plan.status != "pending"
        or len(tasks) != 1
    ):
        raise ConflictException("已有其他后续计划，不能重复派工")
    task = tasks[0]
    if (
        (task.task_key, task.recipient, task.expected_output_type, task.status)
        != ("system_design", "SolutionArchitect", "system_design", "pending")
        or task.input_configuration_item_ids != [item_id]
        or task.depends_on_task_ids
    ):
        raise ConflictException("已有后续任务与本次设计交接不一致")
    return task
