from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.models.build_run import ACTIVE_BUILD_RUN_STATUSES, BuildRun, BuildRunStage, BuildRunStatus
from app.models.configuration_item import ConfigurationItemType
from app.models.plan import Plan, PlanStatus
from app.models.project import Project
from app.models.task import Task, TaskRecipient, TaskStatus
from app.models.user import User
from app.services import plan as plan_service
from app.services import project as project_service


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


def claim_product_manager_task(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
) -> Task:
    """领取指定的初始需求任务，不执行模型，也不重放已领取的任务。

    当前仅支持无上游任务、无成果输入的 ProductManager / app_spec 任务。
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
        if run.status not in ACTIVE_BUILD_RUN_STATUSES or run.active_slot != 1:
            raise ConflictException("构建任务已结束，不能领取任务")
        if run.status == BuildRunStatus.RUNNING.value and run.stage != BuildRunStage.PM.value:
            raise ConflictException("构建已进入其他阶段，不能领取初始需求任务")

        # 先取得运行的写锁，再检查计划和任务；后续任何失败都会回滚这次状态修改。
        # 条件更新还能防止使用过时状态，并让 SQLite 并发测试不依赖 FOR UPDATE。
        reserved = db.connection().execute(
            update(BuildRun)
            .where(
                BuildRun.project_id == project_id,
                BuildRun.run_id == run_id,
                BuildRun.status == run.status,
                BuildRun.stage == run.stage,
                BuildRun.active_slot == 1,
            )
            .values(status=BuildRunStatus.RUNNING.value, stage=BuildRunStage.PM.value)
        )
        if reserved.rowcount != 1:
            raise ConflictException("构建状态已改变，请重新读取后再领取")
        db.expire(run)

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
            raise BusinessException("该任务不是分配给 ProductManager 的")
        if task.expected_output_type != ConfigurationItemType.APP_SPEC.value:
            raise BusinessException("需求整理任务的预期成果必须是 app_spec")
        if task.status != TaskStatus.PENDING.value:
            raise ConflictException("任务已被领取或已结束，不能重复领取")
        if plan.status not in (PlanStatus.PENDING.value, PlanStatus.RUNNING.value):
            raise ConflictException("计划已结束，不能领取其中的任务")
        if task.depends_on_task_ids or task.input_configuration_item_ids:
            raise BusinessException("当前只支持领取无上游任务、无成果输入的初始需求任务")

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
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(task)
    return task
