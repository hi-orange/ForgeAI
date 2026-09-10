from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class TaskArtifact(Base):
    """Affiliated artifact for a task. The main result remains on TaskResult."""

    __tablename__ = "task_artifact"
    __table_args__ = (
        CheckConstraint(
            "artifact_role IN ('system_design', 'test_report', 'other')",
            name="ck_task_artifact_role",
        ),
        UniqueConstraint("configuration_item_id", name="uq_task_artifact_configuration_item"),
        UniqueConstraint(
            "task_id", "artifact_role", "configuration_item_id", name="uq_task_artifact"
        ),
        {"comment": "任务附属产物；主结果仍由 task_result 保管"},
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="内部主键"
    )
    task_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("task.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属任务的稳定标识",
    )
    configuration_item_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("configuration_item.item_id", ondelete="CASCADE"),
        nullable=False,
        comment="附属产物的 ConfigurationItem 编号",
    )
    artifact_role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="附属角色：system_design / test_report / other",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), nullable=False, comment="登记时间"
    )


@event.listens_for(TaskArtifact, "before_update")
def _guard_task_artifact_update(_mapper: object, _connection: object, target: TaskArtifact) -> None:
    state = sqlalchemy_inspect(target)
    if any(attribute.history.has_changes() for attribute in state.attrs):
        raise ValueError("已登记的附属产物不可修改；请登记新的产物")
