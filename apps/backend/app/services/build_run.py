from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException, NotFoundException
from app.models.build_run import ACTIVE_BUILD_RUN_STATUSES, BuildRun, BuildRunStatus
from app.models.user import User
from app.services import project as project_service


def _active_build_run(db: Session, project_id: int) -> BuildRun | None:
    """查找仍占用项目构建名额的 queued/running 任务。"""

    return db.scalar(
        select(BuildRun).where(
            BuildRun.project_id == project_id,
            BuildRun.status.in_(ACTIVE_BUILD_RUN_STATUSES),
        )
    )


def create_build_run(db: Session, user: User, project_id: int) -> BuildRun:
    """创建一张 queued 构建工单，但暂不启动真正的构建 Worker。"""

    project_service.get_user_project(db, user, project_id)
    if _active_build_run(db, project_id) is not None:
        raise ConflictException("项目已有构建任务正在运行")
    build_run = BuildRun(
        project_id=project_id,
        run_id=f"run_{uuid4().hex}",
        status=BuildRunStatus.QUEUED.value,
        active_slot=1,
    )
    db.add(build_run)

    # 两个请求仍可能同时通过上面的快速检查，所以最终必须依赖数据库唯一约束。
    try:
        db.commit()
    except IntegrityError as exc:
        # 回滚失败事务后重新查询：如果另一个请求已经成功，就统一转换为 409。
        db.rollback()
        if _active_build_run(db, project_id) is not None:
            raise ConflictException("项目已有构建任务正在运行") from exc
        raise

    db.refresh(build_run)
    return build_run


def get_user_build_run(db: Session, user: User, project_id: int, run_id: str) -> BuildRun:
    """查询当前用户指定项目下的一次构建任务。"""
    project_service.get_user_project(db, user, project_id)
    build_run = db.scalar(
        select(BuildRun).where(
            BuildRun.project_id == project_id,
            BuildRun.run_id == run_id,
        )
    )
    if build_run is None:
        raise NotFoundException("构建任务不存在")
    return build_run
