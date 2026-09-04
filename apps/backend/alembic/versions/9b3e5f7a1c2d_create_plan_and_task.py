"""create plan and task

Revision ID: 9b3e5f7a1c2d
Revises: 7c1f3e9a4d2b
Create Date: 2026-09-04 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9b3e5f7a1c2d"
down_revision: str | Sequence[str] | None = "7c1f3e9a4d2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan",
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
            comment="内部主键；对外请用 plan_id",
        ),
        sa.Column(
            "plan_id", sa.String(40), nullable=False, comment="稳定计划标识，形如 plan_<uuid>"
        ),
        sa.Column("project_id", sa.Integer(), nullable=False, comment="所属项目 ID"),
        sa.Column(
            "build_run_id",
            sa.String(40),
            nullable=False,
            comment="所属 BuildRun.run_id；不是内部自增主键",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment="调用方明确指定的计划版本；同一运行内唯一",
        ),
        sa.Column(
            "cause_message_id",
            sa.Integer(),
            nullable=False,
            comment="触发计划的同项目用户消息 ID",
        ),
        sa.Column(
            "definition_hash",
            sa.String(64),
            nullable=False,
            comment="规范化计划与全部任务的 SHA-256，用于幂等校验",
        ),
        sa.Column(
            "status",
            sa.String(16),
            server_default="pending",
            nullable=False,
            comment="pending/running/succeeded/failed/cancelled；保存时为 pending",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
            comment="状态更新时间",
        ),
        sa.CheckConstraint("version > 0", name="ck_plan_positive_version"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_plan_status",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["build_run_id"], ["build_run.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cause_message_id"], ["project_message.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", name="uq_plan_plan_id"),
        sa.UniqueConstraint("build_run_id", "version", name="uq_plan_run_version"),
        comment="BuildRun 下的不可改写计划版本",
    )
    op.create_index("ix_plan_project_run", "plan", ["project_id", "build_run_id"], unique=False)

    op.create_table(
        "task",
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
            comment="内部主键；对外请用 task_id",
        ),
        sa.Column(
            "task_id", sa.String(40), nullable=False, comment="稳定任务标识，形如 task_<uuid>"
        ),
        sa.Column(
            "plan_id",
            sa.String(40),
            nullable=False,
            comment="所属计划的 plan_id；通过计划确定项目和运行",
        ),
        sa.Column(
            "task_key",
            sa.String(64),
            nullable=False,
            comment="调用方在计划内使用的唯一短名，保存时用于解析依赖",
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            comment="计划内从 1 开始的展示顺序；不是强制执行顺序",
        ),
        sa.Column(
            "recipient",
            sa.String(32),
            nullable=False,
            comment="唯一接收岗位：ProductManager 等 TaskRecipient 值",
        ),
        sa.Column("title", sa.String(200), nullable=False, comment="任务标题"),
        sa.Column("instructions", sa.Text(), nullable=False, comment="本任务具体要完成的工作"),
        sa.Column(
            "expected_output_type",
            sa.String(32),
            nullable=False,
            comment="预期 ConfigurationItem 语义类型；不是已生成结果",
        ),
        sa.Column(
            "input_configuration_item_ids",
            sa.JSON(),
            nullable=False,
            comment="已固定的准确输入 item_id；无输入时为空列表",
        ),
        sa.Column(
            "depends_on_task_ids",
            sa.JSON(),
            nullable=False,
            comment="同一计划内的直接依赖 task_id；不存模糊名称或最新版本引用",
        ),
        sa.Column(
            "status",
            sa.String(16),
            server_default="pending",
            nullable=False,
            comment="pending/running/succeeded/failed/cancelled；保存不会执行任务",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
            comment="状态更新时间",
        ),
        sa.CheckConstraint("position > 0", name="ck_task_positive_position"),
        sa.CheckConstraint(
            "recipient IN ('ProductManager', 'SolutionArchitect', "
            "'SoftwareEngineer', 'QAEngineer')",
            name="ck_task_recipient",
        ),
        sa.CheckConstraint(
            "expected_output_type IN ('app_spec', 'system_design', 'code', 'test_report')",
            name="ck_task_expected_output_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_task_status",
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["plan.plan_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_task_task_id"),
        sa.UniqueConstraint("plan_id", "task_key", name="uq_task_plan_key"),
        sa.UniqueConstraint("plan_id", "position", name="uq_task_plan_position"),
        comment="计划内的岗位定向工作单；不使用 ProjectMessage 作为内部队列",
    )


def downgrade() -> None:
    op.drop_table("task")
    op.drop_index("ix_plan_project_run", table_name="plan")
    op.drop_table("plan")
