"""create_project

Revision ID: 7d0a4f5b7c50
Revises: 76e45cd94473
Create Date: 2026-08-07 10:17:44.844757

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7d0a4f5b7c50"
down_revision: str | Sequence[str] | None = "76e45cd94473"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="draft", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_project_id"), "project", ["id"], unique=False)
    op.create_index(op.f("ix_project_user_id"), "project", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_project_user_id"), table_name="project")
    op.drop_index(op.f("ix_project_id"), table_name="project")
    op.drop_table("project")
