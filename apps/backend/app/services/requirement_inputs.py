from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, ConflictException
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.requirement_clarification import RequirementClarification
from app.models.task import Task
from app.models.task_result import TaskResult
from app.schemas.app_spec import APP_SPEC_SCHEMA_VERSION, AppSpec


def read_app_spec(item: ConfigurationItem) -> AppSpec:
    """Read supported persisted versions without changing their payload or identity."""
    if item.state != "usable" or item.semantic_type != "app_spec":
        raise ConflictException("需求成果已不可用")
    if item.schema_version not in (1, APP_SPEC_SCHEMA_VERSION):
        raise ConflictException("需求版本暂不支持")
    try:
        return AppSpec.model_validate(item.payload)
    except ValidationError as exc:
        raise ConflictException("需求正文不符合要求") from exc


def load_previous_app_spec(db: Session, task: Task, *, lock: bool = False) -> AppSpec | None:
    """只接受绑定了补充回答的准确旧需求；任意传一个成果编号不能伪装成补充任务。"""

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
    item = row[0]
    return read_app_spec(item)
