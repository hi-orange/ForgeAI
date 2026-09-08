from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class RequirementClarification(Base):
    """一版需求的一次补充回答；问题正文保存在原 app_spec 中。"""

    __tablename__ = "requirement_clarification"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_clarification_task"),
        UniqueConstraint("answer_message_id", name="uq_clarification_answer"),
        UniqueConstraint("followup_plan_id", name="uq_clarification_plan"),
        CheckConstraint(
            "(answer_message_id IS NULL AND followup_plan_id IS NULL) OR "
            "(answer_message_id IS NOT NULL AND followup_plan_id IS NOT NULL)",
            name="ck_clarification_answer_plan",
        ),
    )

    configuration_item_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("configuration_item.item_id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("task.task_id", ondelete="CASCADE"), nullable=False
    )
    answer_message_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("project_message.id", ondelete="CASCADE")
    )
    followup_plan_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("plan.plan_id", ondelete="CASCADE")
    )
