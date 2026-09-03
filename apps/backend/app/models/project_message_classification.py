from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ProjectMessageCategory(StrEnum):
    """ProjectManager 对用户消息作出的唯一分类。"""

    INQUIRY = "inquiry"
    STOP = "stop"
    PRODUCT_CHANGE = "product_change"
    IMPLEMENTATION_REPAIR = "implementation_repair"


class ProjectMessageClassification(Base):
    """一条用户消息的持久化分类结果。"""

    __tablename__ = "project_message_classification"
    __table_args__ = (
        CheckConstraint(
            "category IN ('inquiry', 'stop', 'product_change', 'implementation_repair')",
            name="ck_project_message_classification_category",
        ),
        UniqueConstraint(
            "message_id",
            name="uq_project_message_classification_message_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("project_message.id", ondelete="CASCADE"),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_summary: Mapped[str] = mapped_column(Text, nullable=False)
    classifier_model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
