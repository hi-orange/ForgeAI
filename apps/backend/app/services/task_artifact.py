from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException, NotFoundException
from app.models.configuration_item import ConfigurationItem, ConfigurationItemType
from app.models.plan import Plan
from app.models.task import Task
from app.models.task_artifact import TaskArtifact
from app.models.task_result import TaskResult


class TaskArtifactRole(StrEnum):
    SYSTEM_DESIGN = "system_design"
    TEST_REPORT = "test_report"
    OTHER = "other"


_ROLE_TO_SEMANTIC = {
    TaskArtifactRole.SYSTEM_DESIGN: ConfigurationItemType.SYSTEM_DESIGN.value,
    TaskArtifactRole.TEST_REPORT: ConfigurationItemType.TEST_REPORT.value,
}


def stage_task_side_artifact(
    db: Session,
    *,
    task_id: str,
    configuration_item_id: str,
    artifact_role: TaskArtifactRole,
) -> TaskArtifact:
    """Register an affiliated artifact for a task inside the caller's transaction.

    The main TaskResult remains singular. Side artifacts cannot replace or duplicate it.
    """
    task = db.scalar(select(Task).where(Task.task_id == task_id).with_for_update())
    if task is None:
        raise NotFoundException("任务不存在")
    item = db.scalar(
        select(ConfigurationItem)
        .where(ConfigurationItem.item_id == configuration_item_id)
        .with_for_update()
    )
    if item is None:
        raise NotFoundException("附属产物不存在")
    # Ownership fields are immutable; do not acquire a plan lock after a task lock.
    plan = db.scalar(select(Plan).where(Plan.plan_id == task.plan_id))
    if plan is None or (plan.project_id, plan.build_run_id) != (
        item.project_id,
        item.producer_run_id,
    ):
        raise ConflictException("附属产物必须由当前任务所属项目和构建产生")
    expected_type = _ROLE_TO_SEMANTIC.get(artifact_role)
    if expected_type is not None and item.semantic_type != expected_type:
        raise ConflictException("附属产物类型与角色不匹配")
    main = db.scalar(
        select(TaskResult).where(TaskResult.configuration_item_id == configuration_item_id)
    )
    if main is not None:
        raise ConflictException("主结果不能同时登记为附属产物")
    existing = db.scalar(
        select(TaskArtifact).where(TaskArtifact.configuration_item_id == configuration_item_id)
    )
    if existing is not None:
        if existing.task_id != task_id or existing.artifact_role != artifact_role.value:
            raise ConflictException("该成果已作为其他附属产物登记")
        return existing
    artifact = TaskArtifact(
        task_id=task_id,
        configuration_item_id=configuration_item_id,
        artifact_role=artifact_role.value,
    )
    db.add(artifact)
    db.flush()
    return artifact


def list_task_side_artifacts(db: Session, task_id: str) -> list[TaskArtifact]:
    return list(
        db.scalars(
            select(TaskArtifact)
            .where(TaskArtifact.task_id == task_id)
            .order_by(TaskArtifact.id.asc())
        ).all()
    )
