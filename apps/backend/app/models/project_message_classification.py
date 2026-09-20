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
    """Manager 对用户消息作出的唯一分类。"""

    # 询问进度/解释/确认等，不改变应用行为
    INQUIRY = "inquiry"
    # 明确要求停止、取消或终止当前构建/修改
    STOP = "stop"
    # 首次提出应用，或新增/删除/改变用户可见行为与验收
    PRODUCT_CHANGE = "product_change"
    # 只修复已确认意图下的现有实现，不新增产品要求
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
        {"comment": "用户消息的 Manager 分类结果"},
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
        autoincrement=True,
        comment="内部自增主键",
    )
    message_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("project_message.id", ondelete="CASCADE"),
        nullable=False,
        comment="对应的项目消息 ID；一条消息最多一条分类",
    )
    category: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment=(
            "inquiry=询问不改行为；stop=停止构建；"
            "product_change=产品变更；implementation_repair=修已有实现"
        ),
    )
    decision_summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="一句可审计的分类依据",
    )
    classifier_model: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="当时使用的模型名",
    )
    prompt_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="当时使用的 prompt 版本标识",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        nullable=False,
        comment="分类时间",
    )
