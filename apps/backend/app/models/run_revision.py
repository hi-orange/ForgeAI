from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class RunRevision(Base):
    """Explicit lineage from an immutable completed run to a new revision run."""

    __tablename__ = "run_revision"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('product_change', 'implementation_repair')",
            name="ck_run_revision_kind",
        ),
        UniqueConstraint("target_run_id", name="uq_run_revision_target_run"),
        UniqueConstraint("cause_message_id", name="uq_run_revision_cause_message"),
    )

    revision_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_run_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("build_run.run_id", ondelete="RESTRICT"), nullable=False
    )
    target_run_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("build_run.run_id", ondelete="CASCADE"), nullable=False
    )
    cause_message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project_message.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    baseline_app_spec_item_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("configuration_item.item_id", ondelete="RESTRICT"), nullable=False
    )
    baseline_code_item_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("configuration_item.item_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), nullable=False
    )
