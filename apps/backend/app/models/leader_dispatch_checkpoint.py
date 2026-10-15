"""Mutable pre-dispatch progress, separate from completed role executions."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class LeaderDispatchCheckpoint(Base):
    __tablename__ = "leader_dispatch_checkpoint"

    approved_item_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("configuration_item.item_id", ondelete="CASCADE"), primary_key=True
    )
    build_run_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("build_run.run_id", ondelete="CASCADE"), nullable=False, index=True
    )
    frozen_input: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    tool_loop: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    lease_owner: Mapped[str | None] = mapped_column(String(40))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
