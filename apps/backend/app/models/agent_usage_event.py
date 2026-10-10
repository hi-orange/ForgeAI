from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class AgentUsageEvent(Base):
    """Measured latency and provider usage for one model turn."""

    __tablename__ = "agent_usage_event"

    event_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    build_run_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("build_run.run_id", ondelete="SET NULL"), index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("task.task_id", ondelete="SET NULL"), index=True
    )
    execution_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("task_execution.execution_id", ondelete="SET NULL")
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(100))
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost_microusd: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), nullable=False
    )
