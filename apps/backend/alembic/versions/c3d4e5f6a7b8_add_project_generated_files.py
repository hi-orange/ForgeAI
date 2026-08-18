"""add project generated files

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-18 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("project", sa.Column("generated_files", sa.Text(), nullable=True))
    op.add_column("project", sa.Column("build_error", sa.Text(), nullable=True))
    op.add_column("project", sa.Column("built_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("project", "built_at")
    op.drop_column("project", "build_error")
    op.drop_column("project", "generated_files")
