from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ProjectStatus(StrEnum):
    """项目自身是否已经拥有可用版本，不表示构建任务是否正在执行。"""

    DRAFT = "draft"
    AVAILABLE = "available"


class Project(Base):
    """用户拥有的一个应用项目。"""

    __tablename__ = "project"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'available')",
            name="ck_project_status",
        ),
        {"comment": "用户拥有的应用项目"},
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
        autoincrement=True,
        comment="内部自增主键",
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属用户 ID",
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="项目名称")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="项目描述，可空")
    prompt: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="创建时的初始需求文本",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ProjectStatus.DRAFT.value,
        server_default=ProjectStatus.DRAFT.value,
        comment="draft=尚无可用版本；available=已有可用版本",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="最后更新时间",
    )
