from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, UniqueConstraint, event, func
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class TaskResult(Base):
    """任务的一次正式产出；只保存关联和来源，正文由 ConfigurationItem 保管。"""

    __tablename__ = "task_result"
    __table_args__ = (
        UniqueConstraint("configuration_item_id", name="uq_task_result_configuration_item"),
        {"comment": "任务的不可覆盖产出记录；每个任务最多登记一次"},
    )

    task_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("task.task_id", ondelete="CASCADE"),
        primary_key=True,
        comment="产出任务的稳定标识；同时作为唯一键",
    )
    configuration_item_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("configuration_item.item_id", ondelete="CASCADE"),
        nullable=False,
        comment="本任务产出的正式成果编号",
    )
    result_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="包含正文和来源的规范化提交 SHA-256；用于幂等校验"
    )
    source_message_ids: Mapped[list[int]] = mapped_column(
        MutableList.as_mutable(JSON), nullable=False, comment="模型实际使用的消息编号，按原顺序排列"
    )
    context_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="是否因上下文预算省略了较早历史"
    )
    model: Mapped[str] = mapped_column(String(100), nullable=False, comment="生成结果使用的模型")
    prompt_version: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="生成结果使用的提示词版本"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), nullable=False, comment="登记时间"
    )


@event.listens_for(TaskResult, "before_update")
def _guard_task_result_update(_mapper: object, _connection: object, target: TaskResult) -> None:
    state = sqlalchemy_inspect(target)
    if any(attribute.history.has_changes() for attribute in state.attrs):
        raise ValueError("已登记的任务产出不可修改；请创建新的任务")
