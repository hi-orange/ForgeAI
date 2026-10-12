from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class WorkflowJob(Base):
    """Database-backed work item claimed with a renewable worker lease."""

    __tablename__ = "workflow_job"
    __table_args__ = (
        CheckConstraint(
            "job_type IN ('product_manager', 'architect', 'engineering', "
            "'quality', 'security', 'deployment')",
            name="ck_workflow_job_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled')",
            name="ck_workflow_job_status",
        ),
        CheckConstraint("attempt >= 0", name="ck_workflow_job_attempt"),
        CheckConstraint("max_attempts > 0", name="ck_workflow_job_max_attempts"),
    )

    job_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    job_type: Mapped[str] = mapped_column(String(24), nullable=False)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    build_run_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("build_run.run_id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("task.task_id", ondelete="CASCADE"), index=True
    )
    execution_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("task_execution.execution_id", ondelete="SET NULL"), index=True
    )
    deployment_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("deployment.deployment_id", ondelete="CASCADE"), index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), nullable=False, index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
