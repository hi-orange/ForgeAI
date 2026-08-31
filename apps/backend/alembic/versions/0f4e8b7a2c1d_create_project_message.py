"""create project message

Revision ID: 0f4e8b7a2c1d
Revises: a7b8c9d0e1f2
Create Date: 2026-08-31 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0f4e8b7a2c1d"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_initial_project_messages() -> None:
    project = sa.table(
        "project",
        sa.column("id", sa.Integer()),
        sa.column("prompt", sa.Text()),
        sa.column("created_at", sa.DateTime()),
    )
    project_message = sa.table(
        "project_message",
        sa.column("project_id", sa.Integer()),
        sa.column("sequence", sa.Integer()),
        sa.column("sender", sa.String(length=16)),
        sa.column("content", sa.Text()),
        sa.column("client_message_id", sa.String(length=100)),
        sa.column("created_at", sa.DateTime()),
    )

    connection = op.get_bind()
    rows = connection.execute(
        sa.select(project.c.id, project.c.prompt, project.c.created_at).where(
            project.c.prompt.is_not(None)
        )
    ).mappings()

    batch: list[dict] = []
    for row in rows:
        prompt = row["prompt"].strip() if row["prompt"] else ""
        if not prompt:
            continue
        batch.append(
            {
                "project_id": row["id"],
                "sequence": 1,
                "sender": "user",
                "content": prompt,
                "client_message_id": f"project:{row['id']}:initial",
                "created_at": row["created_at"],
            }
        )
        if len(batch) == 500:
            connection.execute(project_message.insert(), batch)
            batch.clear()

    if batch:
        connection.execute(project_message.insert(), batch)


def upgrade() -> None:
    op.create_table(
        "project_message",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("sender", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("client_message_id", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_project_message_positive_sequence"),
        sa.CheckConstraint(
            "sender IN ('user', 'assistant')",
            name="ck_project_message_sender",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "client_message_id",
            name="uq_project_message_project_client_message_id",
        ),
        sa.UniqueConstraint(
            "project_id",
            "sequence",
            name="uq_project_message_project_sequence",
        ),
    )
    op.create_index(op.f("ix_project_message_id"), "project_message", ["id"], unique=False)
    _backfill_initial_project_messages()


def downgrade() -> None:
    op.drop_index(op.f("ix_project_message_id"), table_name="project_message")
    op.drop_table("project_message")
