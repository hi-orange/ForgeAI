from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class BuildRunStatus(StrEnum):
    """一次构建任务的生命周期状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class BuildRunStage(StrEnum):
    """任务处于 running 时，当前正在工作的处理阶段。"""

    PM = "pm"
    ARCHITECT = "architect"
    DEVELOPER = "developer"
    QA = "qa"
    RUNTIME = "runtime"


ACTIVE_BUILD_RUN_STATUSES = (
    BuildRunStatus.QUEUED.value,
    BuildRunStatus.RUNNING.value,
)


class BuildRun(Base):
    """持久化的一次构建工单。

    BuildRun 只负责记录任务身份、状态和互斥关系，不直接生成代码。
    """

    __tablename__ = "build_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_build_run_status",
        ),
        CheckConstraint(
            "stage IS NULL OR stage IN ('pm', 'architect', 'developer', 'qa', 'runtime')",
            name="ck_build_run_stage",
        ),
        CheckConstraint(
            # queued 还没有处理阶段；running 必须告诉调用方当前执行到哪一步。
            "((status = 'queued' AND stage IS NULL) "
            "OR (status = 'running' AND stage IS NOT NULL) "
            "OR status IN ('succeeded', 'failed'))",
            name="ck_build_run_stage_by_status",
        ),
        CheckConstraint(
            # active_slot=1 表示任务仍占用项目构建名额；终态必须释放为 NULL。
            "((status IN ('queued', 'running') AND active_slot IS NOT NULL "
            "AND active_slot = 1) "
            "OR (status IN ('succeeded', 'failed') AND active_slot IS NULL))",
            name="ck_build_run_active_slot",
        ),
        # 唯一约束是并发兜底：同一项目最多只能有一个 active_slot=1。
        UniqueConstraint("project_id", "active_slot", name="uq_build_run_project_active_slot"),
        UniqueConstraint("run_id", name="uq_build_run_run_id"),
        Index("ix_build_run_project_status", "project_id", "status"),
        {"comment": "一次构建任务工单"},
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
        autoincrement=True,
        comment="内部自增主键；对外请用 run_id",
    )
    project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属项目 ID",
    )
    run_id: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment="对外任务标识，形如 run_<uuid>",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=BuildRunStatus.QUEUED.value,
        server_default=BuildRunStatus.QUEUED.value,
        comment="queued/running/succeeded/failed",
    )
    stage: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="running 时的当前阶段；queued 时为空",
    )
    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="失败时的错误信息",
    )
    active_slot: Mapped[int | None] = mapped_column(
        SmallInteger,
        nullable=True,
        default=1,
        server_default="1",
        comment="占用项目构建名额时为 1；终态为 NULL",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="最后更新时间",
    )
