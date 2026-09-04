from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class PlanStatus(StrEnum):
    """计划的状态词表；保存时为 pending，开始领取任务时转为 running。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Plan(Base):
    """一个 BuildRun 下不可原地改写的计划版本。"""

    __tablename__ = "plan"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_plan_positive_version"),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_plan_status",
        ),
        UniqueConstraint("plan_id", name="uq_plan_plan_id"),
        UniqueConstraint("build_run_id", "version", name="uq_plan_run_version"),
        Index("ix_plan_project_run", "project_id", "build_run_id"),
        {"comment": "BuildRun 下的不可改写计划版本"},
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="内部主键；对外请用 plan_id"
    )
    plan_id: Mapped[str] = mapped_column(
        String(40), nullable=False, comment="稳定计划标识，形如 plan_<uuid>"
    )
    project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属项目 ID",
    )
    build_run_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("build_run.run_id", ondelete="CASCADE"),
        nullable=False,
        comment="所属 BuildRun.run_id；不是内部自增主键",
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="调用方明确指定的计划版本；同一运行内唯一"
    )
    cause_message_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("project_message.id", ondelete="CASCADE"),
        nullable=False,
        comment="触发计划的同项目用户消息 ID",
    )
    definition_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="规范化计划与全部任务的 SHA-256，用于幂等校验"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=PlanStatus.PENDING.value,
        server_default=PlanStatus.PENDING.value,
        comment="pending/running/succeeded/failed/cancelled；保存时为 pending",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="状态更新时间",
    )


@event.listens_for(Plan, "before_update")
def _guard_plan_definition(_mapper: object, _connection: object, target: Plan) -> None:
    state = sqlalchemy_inspect(target)
    immutable_fields = (
        "id",
        "plan_id",
        "project_id",
        "build_run_id",
        "version",
        "cause_message_id",
        "definition_hash",
        "created_at",
    )
    if any(state.attrs[name].history.has_changes() for name in immutable_fields):
        raise ValueError("已保存的 Plan 定义不可修改；请创建新版本")
