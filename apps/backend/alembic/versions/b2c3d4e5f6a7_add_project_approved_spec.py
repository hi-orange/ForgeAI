"""add project approved spec

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-18 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("project", sa.Column("approved_spec", sa.Text(), nullable=True))
    op.add_column("project", sa.Column("approved_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("project", "approved_at")
    op.drop_column("project", "approved_spec")
