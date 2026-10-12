from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, event, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class WorkflowEvent(Base):
    """Immutable fact emitted by workflow orchestration or one execution attempt."""

    __tablename__ = "workflow_event"
    __table_args__ = (
        UniqueConstraint("stream_key", "sequence", name="uq_workflow_event_stream_sequence"),
    )

    event_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    stream_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    build_run_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("build_run.run_id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("task.task_id", ondelete="SET NULL"), index=True
    )
    execution_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("task_execution.execution_id", ondelete="SET NULL"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("workflow_job.job_id", ondelete="SET NULL"), index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), nullable=False, index=True
    )


@event.listens_for(WorkflowEvent, "before_update")
def _reject_event_update(*_args: object) -> None:
    raise ValueError("WorkflowEvent 是追加式事实，不允许修改")


@event.listens_for(WorkflowEvent, "before_delete")
def _reject_event_delete(*_args: object) -> None:
    raise ValueError("WorkflowEvent 是追加式事实，不允许删除")
