from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException, NotFoundException
from app.models.build_run import BuildRun
from app.models.plan import Plan
from app.models.project import Project
from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.models.user import User

EXECUTION_LEASE = timedelta(minutes=10)


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def lock_run(db: Session, user: User, project_id: int, run_id: str) -> BuildRun:
    """统一锁顺序：项目 → 运行；SQLite 也通过无状态变更的 UPDATE 串行化。"""

    project = db.scalar(
        select(Project)
        .where(Project.id == project_id, Project.user_id == user.id)
        .with_for_update()
    )
    if project is None:
        raise NotFoundException("项目不存在")
    locked = db.connection().execute(
        update(BuildRun)
        .where(BuildRun.project_id == project_id, BuildRun.run_id == run_id)
        .values(updated_at=BuildRun.updated_at)
    )
    if locked.rowcount != 1:
        raise NotFoundException("构建任务不存在")
    run = db.scalar(
        select(BuildRun)
        .where(BuildRun.run_id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert run is not None
    return run


def latest_execution(db: Session, task_id: str, *, lock: bool = False) -> TaskExecution | None:
    statement = (
        select(TaskExecution)
        .where(TaskExecution.task_id == task_id)
        .order_by(TaskExecution.attempt.desc())
        .limit(1)
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return db.scalar(statement)


def require_execution(
    db: Session, task_id: str, execution_id: str | None, *, lock: bool = False
) -> TaskExecution | None:
    """旧服务调用仅在任务从未进入受管执行时兼容；不能绕过已存在的执行凭证。"""

    current = latest_execution(db, task_id, lock=lock)
    if current is None and execution_id is None:
        return None
    if (
        current is None
        or current.execution_id != execution_id
        or current.status != "running"
        or current.active_slot != 1
        or current.expires_at <= utc_now()
    ):
        raise ConflictException("执行编号已失效或任务正在由其他执行处理")
    return current


def start_execution(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    recovery_execution_id: str | None = None,
) -> TaskExecution:
    try:
        run = lock_run(db, user, project_id, run_id)
        row = db.execute(
            select(Task, Plan)
            .join(Plan, Task.plan_id == Plan.plan_id)
            .where(
                Task.task_id == task_id, Plan.project_id == project_id, Plan.build_run_id == run_id
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            raise NotFoundException("任务不存在")
        task, plan = row
        if (
            run.status != "running"
            or run.active_slot != 1
            or task.status != "running"
            or plan.status != "running"
        ):
            raise ConflictException("当前任务没有执行资格")
        current = latest_execution(db, task_id, lock=True)
        now = utc_now()
        if current is not None:
            if current.status == "running" and current.expires_at > now:
                raise ConflictException("任务正在处理，请勿重复启动")
            if current.execution_id != recovery_execution_id or current.status == "succeeded":
                raise ConflictException("请使用当前执行编号显式恢复任务")
            if current.status == "running":
                current.status, current.active_slot, current.finished_at = "superseded", None, now
                db.flush()
        elif recovery_execution_id is not None:
            raise ConflictException("没有对应的执行记录可恢复")
        execution = TaskExecution(
            execution_id=f"exec_{uuid4().hex}",
            task_id=task_id,
            attempt=current.attempt + 1 if current else 1,
            status="running",
            active_slot=1,
            started_at=now,
            expires_at=now + EXECUTION_LEASE,
            draft=current.draft if current else None,
        )
        db.add(execution)
        db.commit()
        db.refresh(execution)
        return execution
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("任务已被另一执行领取") from exc
    except Exception:
        db.rollback()
        raise


def fail_execution(
    db: Session,
    task_id: str,
    execution_id: str,
    *,
    error: str = "任务未完成，可重试恢复；已保存的成果不受影响。",
) -> None:
    # 只结束自己的执行；旧执行的异常不能把恢复后的执行标记为失败。
    db.execute(
        update(TaskExecution)
        .where(
            TaskExecution.task_id == task_id,
            TaskExecution.execution_id == execution_id,
            TaskExecution.status == "running",
            TaskExecution.active_slot == 1,
        )
        .values(
            status="failed",
            active_slot=None,
            finished_at=utc_now(),
            error=error,
        )
    )
    db.commit()
