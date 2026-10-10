from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException
from app.models.build_run import BuildRun, BuildRunStatus
from app.models.plan import Plan, PlanStatus
from app.models.task import Task, TaskStatus
from app.models.task_execution import TaskExecution
from app.models.user import User
from app.services.engineering import mark_engineering_inactive
from app.services.task_execution import lock_run


def cancel_active_run(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    *,
    reason: str = "用户取消了本次构建。",
) -> BuildRun:
    """Atomically fence every active owner before releasing the BuildRun slot."""

    run = lock_run(db, user, project_id, run_id)
    if run.status == BuildRunStatus.CANCELLED.value:
        return run
    if run.status not in {BuildRunStatus.QUEUED.value, BuildRunStatus.RUNNING.value}:
        raise ConflictException("构建已经结束，不能取消")

    plans = list(
        db.scalars(
            select(Plan)
            .where(
                Plan.project_id == project_id,
                Plan.build_run_id == run_id,
                Plan.status.in_((PlanStatus.PENDING.value, PlanStatus.RUNNING.value)),
            )
            .with_for_update()
        ).all()
    )
    plan_ids = [plan.plan_id for plan in plans]
    tasks = (
        list(
            db.scalars(
                select(Task)
                .where(
                    Task.plan_id.in_(plan_ids),
                    Task.status.in_((TaskStatus.PENDING.value, TaskStatus.RUNNING.value)),
                )
                .with_for_update()
            ).all()
        )
        if plan_ids
        else []
    )
    task_ids = [task.task_id for task in tasks]
    executions = (
        list(
            db.scalars(
                select(TaskExecution)
                .where(
                    TaskExecution.task_id.in_(task_ids),
                    TaskExecution.status == "running",
                    TaskExecution.active_slot == 1,
                )
                .with_for_update()
            ).all()
        )
        if task_ids
        else []
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    execution_ids: list[str] = []
    for execution in executions:
        execution.status = "cancelled"
        execution.active_slot = None
        execution.finished_at = now
        execution.error = reason[:500]
        execution_ids.append(execution.execution_id)
    for task in tasks:
        task.status = TaskStatus.CANCELLED.value
    for plan in plans:
        plan.status = PlanStatus.CANCELLED.value
    run.status = BuildRunStatus.CANCELLED.value
    run.active_slot = None
    run.error = reason
    db.commit()
    db.refresh(run)
    for execution_id in execution_ids:
        mark_engineering_inactive(execution_id)
    return run
