from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class AgentDecisionEvent(Base):
    """Append-oriented evidence for one routing decision and its applied outcome."""

    __tablename__ = "agent_decision_event"
    __table_args__ = (
        CheckConstraint(
            "status IN ('decided', 'applied', 'failed')",
            name="ck_agent_decision_event_status",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    decision_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    build_run_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("build_run.run_id", ondelete="SET NULL"), index=True
    )
    message_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("project_message.id", ondelete="SET NULL"), index=True
    )
    report_item_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("configuration_item.item_id", ondelete="SET NULL")
    )
    category: Mapped[str | None] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient: Mapped[str | None] = mapped_column(String(32))
    source_artifact_ids: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    required_inputs: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    risk_flags: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    decision_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="decided")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), nullable=False
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
