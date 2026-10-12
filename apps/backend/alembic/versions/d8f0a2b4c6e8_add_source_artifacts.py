"""add durable source artifacts

Revision ID: d8f0a2b4c6e8
Revises: c6e8a0b2d4f6
Create Date: 2026-10-12 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d8f0a2b4c6e8"
down_revision: str | Sequence[str] | None = "c6e8a0b2d4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_artifact",
        sa.Column("artifact_id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("storage_provider", sa.String(32), nullable=False),
        sa.Column("object_key", sa.String(500), nullable=False, unique=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("artifact_format", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name="ck_source_artifact_size_nonnegative",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "project_id",
            "source_hash",
            name="uq_source_artifact_project_hash",
        ),
    )
    op.create_index("ix_source_artifact_project_id", "source_artifact", ["project_id"])
    op.create_index("ix_source_artifact_source_hash", "source_artifact", ["source_hash"])


def downgrade() -> None:
    op.drop_table("source_artifact")
