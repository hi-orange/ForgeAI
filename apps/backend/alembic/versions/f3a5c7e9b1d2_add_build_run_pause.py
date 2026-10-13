"""add persistent build run pause marker

Revision ID: f3a5c7e9b1d2
Revises: e0a2c4d6f8b0
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a5c7e9b1d2"
down_revision: str | None = "e0a2c4d6f8b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("build_run") as batch_op:
        batch_op.add_column(sa.Column("paused_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("build_run") as batch_op:
        batch_op.drop_column("paused_at")
