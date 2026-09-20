from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.models.build_run import BuildRun, BuildRunStage
from app.models.configuration_item import ConfigurationItemType
from app.models.plan import Plan, PlanStatus
from app.models.project import Project
from app.models.task import Task, TaskRecipient, TaskStatus
from app.models.task_result import TaskResult
from app.models.user import User
from app.services import build_run as build_run_service
from app.services import plan as plan_service
from app.services import project as project_service


def stage_running(db: Session, task: Task) -> None:
    """Atomically claim one pending task without committing."""

    if task.status != TaskStatus.PENDING.value:
        raise ConflictException("任务已被领取或已结束，不能重复领取")
    claimed = db.connection().execute(
        update(Task)
        .where(
            Task.task_id == task.task_id,
            Task.plan_id == task.plan_id,
            Task.status == TaskStatus.PENDING.value,
        )
        .values(status=TaskStatus.RUNNING.value)
    )
    if claimed.rowcount != 1:
        raise ConflictException("任务已被其他请求领取或状态已改变")
    db.expire(task)


def stage_succeeded(task: Task, *, allow_pending: bool = False) -> None:
    """Mark a task successful; user-authored tasks may complete without an execution lease."""

    allowed = {TaskStatus.RUNNING.value}
    if allow_pending:
        allowed.add(TaskStatus.PENDING.value)
    if task.status not in allowed:
        raise ConflictException("当前任务状态不能标记为完成")
    task.status = TaskStatus.SUCCEEDED.value


def stage_cancelled(task: Task) -> None:
    """Cancel only an unclaimed task without committing."""

    if task.status != TaskStatus.PENDING.value:
        raise ConflictException("只能取消尚未领取的任务")
    task.status = TaskStatus.CANCELLED.value


def list_user_plan_tasks(db: Session, user: User, project_id: int, plan_id: str) -> list[Task]:
    """只查询任务账本，不领取任务、不把查询结果当成待执行队列。"""

    plan_service.get_user_plan(db, user, project_id, plan_id)
    return list(
        db.scalars(select(Task).where(Task.plan_id == plan_id).order_by(Task.position)).all()
    )


def get_user_task(db: Session, user: User, project_id: int, task_id: str) -> Task:
    project_service.get_user_project(db, user, project_id)
    task = db.scalar(
        select(Task)
        .join(Plan, Task.plan_id == Plan.plan_id)
        .where(Plan.project_id == project_id, Task.task_id == task_id)
    )
    if task is None:
        raise NotFoundException("任务不存在")
    return task


def get_user_task_result(db: Session, user: User, project_id: int, task_id: str) -> TaskResult:
    """读取任务已经登记的产出关联；不查找模糊的最新成果。"""

    get_user_task(db, user, project_id, task_id)
    result = db.get(TaskResult, task_id)
    if result is None:
        raise NotFoundException("任务尚未登记产出")
    return result


def claim_product_manager_task(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
) -> Task:
    """领取指定的初始或补充需求任务，不执行模型，也不重放已领取的任务。

    补充任务必须明确关联原 app_spec 和用户回答。
    返回的 Task 仍通过原 plan_id 引用 Plan.cause_message_id，不另选最新需求。
    本入口提交领取事务；崩溃后的重新派发与执行恢复不在本步处理。
    """

    try:
        # 沿用先锁项目的顺序，同时按所有者过滤，避免暴露他人的任务。
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
        if task.recipient != TaskRecipient.PRODUCT_MANAGER.value:
            raise BusinessException("该任务不是分配给 Product Manager 的")
        if task.expected_output_type != ConfigurationItemType.APP_SPEC.value:
            raise BusinessException("需求整理任务的预期成果必须是 app_spec")
        if task.status != TaskStatus.PENDING.value:
            raise ConflictException("任务已被领取或已结束，不能重复领取")
        if plan.status not in (PlanStatus.PENDING.value, PlanStatus.RUNNING.value):
            raise ConflictException("计划已结束，不能领取其中的任务")
        from app.services.app_spec import load_previous_app_spec

        load_previous_app_spec(db, task, lock=True)

        build_run_service.stage_running(
            db,
            run,
            stage=BuildRunStage.PM,
            allowed_running_stages={BuildRunStage.PM},
        )
        plan_service.stage_running(db, plan)
        stage_running(db, task)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(task)
    return task
