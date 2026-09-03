"""create project message classification

Revision ID: 2a6c9e4f1b7d
Revises: 0f4e8b7a2c1d
Create Date: 2026-09-03 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "2a6c9e4f1b7d"
down_revision: str | Sequence[str] | None = "0f4e8b7a2c1d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_message_classification",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("decision_summary", sa.Text(), nullable=False),
        sa.Column("classifier_model", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "category IN ('inquiry', 'stop', 'product_change', 'implementation_repair')",
            name="ck_project_message_classification_category",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["project_message.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "message_id",
            name="uq_project_message_classification_message_id",
        ),
    )
    op.create_index(
        op.f("ix_project_message_classification_id"),
        "project_message_classification",
        ["id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_project_message_classification_id"),
        table_name="project_message_classification",
    )
    op.drop_table("project_message_classification")
