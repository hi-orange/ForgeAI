"""add_project_prd

Revision ID: a1b2c3d4e5f6
Revises: 7d0a4f5b7c50
Create Date: 2026-08-11 14:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "7d0a4f5b7c50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("project", sa.Column("prd", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("project", "prd")
