"""Persist approved Leader routing progress and its execution fence.

Revision ID: a4b6c8d0e2f4
Revises: f3a5c7e9b1d2
"""

import sqlalchemy as sa

from alembic import op

revision = "a4b6c8d0e2f4"
down_revision = "f3a5c7e9b1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "leader_dispatch_checkpoint",
        sa.Column("approved_item_id", sa.String(40), nullable=False),
        sa.Column("build_run_id", sa.String(40), nullable=False),
        sa.Column("frozen_input", sa.JSON(), nullable=False),
        sa.Column("tool_loop", sa.JSON(), nullable=False),
        sa.Column("lease_owner", sa.String(40), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["approved_item_id"], ["configuration_item.item_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["build_run_id"], ["build_run.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("approved_item_id"),
    )
    op.create_index(
        "ix_leader_dispatch_checkpoint_build_run_id", "leader_dispatch_checkpoint", ["build_run_id"]
    )


def downgrade() -> None:
    op.drop_table("leader_dispatch_checkpoint")
