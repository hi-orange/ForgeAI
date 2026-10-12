from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class SecurityReport(Base):
    """Immutable, platform-produced security evidence for one exact code artifact."""

    __tablename__ = "security_report"
    __table_args__ = (
        CheckConstraint(
            "conclusion IN ('passed', 'failed', 'blocked')", name="ck_security_report_conclusion"
        ),
        UniqueConstraint("code_item_id", "scanner_version", name="uq_security_report_code_scanner"),
    )
    report_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    build_run_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("build_run.run_id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_item_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("configuration_item.item_id", ondelete="CASCADE"), nullable=False
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scanner_version: Mapped[str] = mapped_column(String(64), nullable=False)
    conclusion: Mapped[str] = mapped_column(String(16), nullable=False)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), nullable=False
    )


@event.listens_for(SecurityReport, "before_update")
def _reject_security_report_update(*_args: object) -> None:
    raise ValueError("SecurityReport 是不可变证据，不允许修改")


@event.listens_for(SecurityReport, "before_delete")
def _reject_security_report_delete(*_args: object) -> None:
    raise ValueError("SecurityReport 是不可变证据，不允许删除")
