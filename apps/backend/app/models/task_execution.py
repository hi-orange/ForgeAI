from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class TaskExecution(Base):
    """一次实际执行的凭证；恢复创建新编号，旧执行保留历史且失去提交资格。"""

    __tablename__ = "task_execution"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt", name="uq_task_execution_attempt"),
        UniqueConstraint("task_id", "active_slot", name="uq_task_execution_active"),
        CheckConstraint("attempt > 0", name="ck_task_execution_attempt"),
        CheckConstraint(
            "(status = 'running' AND active_slot = 1 AND active_slot IS NOT NULL) OR "
            "(status IN ('failed', 'succeeded', 'superseded') AND active_slot IS NULL)",
            name="ck_task_execution_status",
        ),
    )

    execution_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("task.task_id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    active_slot: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(String(500))
    draft: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
