from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class AgentEvalLabel(Base):
    """Human or benchmark ground truth for one persisted user message."""

    __tablename__ = "agent_eval_label"
    __table_args__ = (UniqueConstraint("message_id", name="uq_agent_eval_label_message"),)

    label_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project_message.id", ondelete="CASCADE"), nullable=False
    )
    expected_category: Mapped[str] = mapped_column(String(40), nullable=False)
    expected_action: Mapped[str | None] = mapped_column(String(64))
    labeled_by_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), nullable=False
    )
