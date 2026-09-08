"""Add task executions and requirement clarification links.

Revision ID: 5c9d3e7f1a2b
Revises: 4e8a2c6d0f1b
"""

import sqlalchemy as sa

from alembic import op

revision = "5c9d3e7f1a2b"
down_revision = "4e8a2c6d0f1b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_execution",
        sa.Column("execution_id", sa.String(40), primary_key=True),
        sa.Column("task_id", sa.String(40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("active_slot", sa.Integer()),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("error", sa.String(500)),
        sa.Column("draft", sa.JSON(none_as_null=True)),
        sa.ForeignKeyConstraint(["task_id"], ["task.task_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_id", "attempt", name="uq_task_execution_attempt"),
        sa.UniqueConstraint("task_id", "active_slot", name="uq_task_execution_active"),
        sa.CheckConstraint("attempt > 0", name="ck_task_execution_attempt"),
        sa.CheckConstraint(
            "(status = 'running' AND active_slot = 1 AND active_slot IS NOT NULL) OR "
            "(status IN ('failed', 'succeeded', 'superseded') AND active_slot IS NULL)",
            name="ck_task_execution_status",
        ),
    )
    op.create_table(
        "requirement_clarification",
        sa.Column("configuration_item_id", sa.String(40), primary_key=True),
        sa.Column("task_id", sa.String(40), nullable=False),
        sa.Column("answer_message_id", sa.Integer()),
        sa.Column("followup_plan_id", sa.String(40)),
        sa.ForeignKeyConstraint(
            ["configuration_item_id"], ["configuration_item.item_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["task.task_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["answer_message_id"], ["project_message.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["followup_plan_id"], ["plan.plan_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_id", name="uq_clarification_task"),
        sa.UniqueConstraint("answer_message_id", name="uq_clarification_answer"),
        sa.UniqueConstraint("followup_plan_id", name="uq_clarification_plan"),
        sa.CheckConstraint(
            "(answer_message_id IS NULL AND followup_plan_id IS NULL) OR "
            "(answer_message_id IS NOT NULL AND followup_plan_id IS NOT NULL)",
            name="ck_clarification_answer_plan",
        ),
    )


def downgrade() -> None:
    op.drop_table("requirement_clarification")
    op.drop_table("task_execution")
