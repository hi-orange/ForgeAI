"""create task result

Revision ID: 4e8a2c6d0f1b
Revises: 9b3e5f7a1c2d

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4e8a2c6d0f1b"
down_revision: str | Sequence[str] | None = "9b3e5f7a1c2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_result",
        sa.Column(
            "task_id", sa.String(40), nullable=False, comment="产出任务的稳定标识；同时作为唯一键"
        ),
        sa.Column(
            "configuration_item_id",
            sa.String(40),
            nullable=False,
            comment="本任务产出的正式成果编号",
        ),
        sa.Column(
            "result_hash",
            sa.String(64),
            nullable=False,
            comment="包含正文和来源的规范化提交 SHA-256；用于幂等校验",
        ),
        sa.Column(
            "source_message_ids",
            sa.JSON(),
            nullable=False,
            comment="模型实际使用的消息编号，按原顺序排列",
        ),
        sa.Column(
            "context_truncated",
            sa.Boolean(),
            nullable=False,
            comment="是否因上下文预算省略了较早历史",
        ),
        sa.Column("model", sa.String(100), nullable=False, comment="生成结果使用的模型"),
        sa.Column(
            "prompt_version", sa.String(64), nullable=False, comment="生成结果使用的提示词版本"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            comment="登记时间",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["task.task_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["configuration_item_id"], ["configuration_item.item_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("task_id"),
        sa.UniqueConstraint("configuration_item_id", name="uq_task_result_configuration_item"),
        comment="任务的不可覆盖产出记录；每个任务最多登记一次",
    )


def downgrade() -> None:
    op.drop_table("task_result")
