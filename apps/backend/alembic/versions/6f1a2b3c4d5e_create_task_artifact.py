"""Create task_artifact for affiliated task outputs.

Revision ID: 6f1a2b3c4d5e
Revises: 5c9d3e7f1a2b
"""

import sqlalchemy as sa

from alembic import op

revision = "6f1a2b3c4d5e"
down_revision = "5c9d3e7f1a2b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_artifact",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("configuration_item_id", sa.String(length=40), nullable=False),
        sa.Column("artifact_role", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "artifact_role IN ('system_design', 'test_report', 'other')",
            name="ck_task_artifact_role",
        ),
        sa.ForeignKeyConstraint(
            ["configuration_item_id"],
            ["configuration_item.item_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["task.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("configuration_item_id", name="uq_task_artifact_configuration_item"),
        sa.UniqueConstraint(
            "task_id", "artifact_role", "configuration_item_id", name="uq_task_artifact"
        ),
        comment="任务附属产物；主结果仍由 task_result 保管",
    )
    op.create_index("ix_task_artifact_task_id", "task_artifact", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_artifact_task_id", table_name="task_artifact")
    op.drop_table("task_artifact")
