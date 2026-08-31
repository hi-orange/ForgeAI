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


class ProjectMessageSender(StrEnum):
    """对话消息的发送方；内部 Agent 任务不使用这张表。"""

    USER = "user"
    ASSISTANT = "assistant"


class ProjectMessage(Base):
    """项目内不可变、可排序的对话记录。"""

    __tablename__ = "project_message"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_project_message_positive_sequence"),
        CheckConstraint(
            "sender IN ('user', 'assistant')",
            name="ck_project_message_sender",
        ),
        UniqueConstraint(
            "project_id",
            "sequence",
            name="uq_project_message_project_sequence",
        ),
        UniqueConstraint(
            "project_id",
            "client_message_id",
            name="uq_project_message_project_client_message_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    sender: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    client_message_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
