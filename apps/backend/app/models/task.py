from datetime import datetime
from enum import StrEnum

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
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class TaskRecipient(StrEnum):
    """本阶段可接收定向任务的岗位；不是 Agent 实例或进程 ID。"""

    PRODUCT_MANAGER = "ProductManager"
    SOLUTION_ARCHITECT = "SolutionArchitect"
    SOFTWARE_ENGINEER = "SoftwareEngineer"
    QA_ENGINEER = "QAEngineer"


class TaskStatus(StrEnum):
    """pending 表示尚未领取；依赖是否满足不另设一个状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(Base):
    """计划中的定向工作单；项目和运行归属由 Plan 确定。"""

    __tablename__ = "task"
    __table_args__ = (
        CheckConstraint("position > 0", name="ck_task_positive_position"),
        CheckConstraint(
            "recipient IN ('ProductManager', 'SolutionArchitect', "
            "'SoftwareEngineer', 'QAEngineer')",
            name="ck_task_recipient",
        ),
        CheckConstraint(
            "expected_output_type IN ('app_spec', 'system_design', 'code', 'test_report')",
            name="ck_task_expected_output_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_task_status",
        ),
        UniqueConstraint("task_id", name="uq_task_task_id"),
        UniqueConstraint("plan_id", "task_key", name="uq_task_plan_key"),
        UniqueConstraint("plan_id", "position", name="uq_task_plan_position"),
        {"comment": "计划内的岗位定向工作单；不使用 ProjectMessage 作为内部队列"},
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="内部主键；对外请用 task_id"
    )
    task_id: Mapped[str] = mapped_column(
        String(40), nullable=False, comment="稳定任务标识，形如 task_<uuid>"
    )
    plan_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("plan.plan_id", ondelete="CASCADE"),
        nullable=False,
        comment="所属计划的 plan_id；通过计划确定项目和运行",
    )
    task_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="调用方在计划内使用的唯一短名，保存时用于解析依赖"
    )
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="计划内从 1 开始的展示顺序；不是强制执行顺序"
    )
    recipient: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="唯一接收岗位：ProductManager 等 TaskRecipient 值"
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="任务标题")
    instructions: Mapped[str] = mapped_column(
        Text, nullable=False, comment="本任务具体要完成的工作"
    )
    expected_output_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="预期 ConfigurationItem 语义类型；不是已生成结果"
    )
    input_configuration_item_ids: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        comment="已固定的准确输入 item_id；无输入时为空列表",
    )
    depends_on_task_ids: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        comment="同一计划内的直接依赖 task_id；不存模糊名称或最新版本引用",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=TaskStatus.PENDING.value,
        server_default=TaskStatus.PENDING.value,
        comment="pending/running/succeeded/failed/cancelled；保存不会执行任务",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="状态更新时间",
    )


@event.listens_for(Task, "before_update")
def _guard_task_definition(_mapper: object, _connection: object, target: Task) -> None:
    state = sqlalchemy_inspect(target)
    immutable_fields = (
        "id",
        "task_id",
        "plan_id",
        "task_key",
        "position",
        "recipient",
        "title",
        "instructions",
        "expected_output_type",
        "input_configuration_item_ids",
        "depends_on_task_ids",
        "created_at",
    )
    if any(state.attrs[name].history.has_changes() for name in immutable_fields):
        raise ValueError("已保存的 Task 定义不可修改；请在新计划版本中创建任务")
