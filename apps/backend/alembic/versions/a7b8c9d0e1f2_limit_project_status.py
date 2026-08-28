"""limit project status to draft and available

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-28 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 新构建体系没有旧状态对应的可用 Revision，因此统一回到 draft。
    op.execute(
        sa.text("UPDATE project SET status = 'draft' WHERE status NOT IN ('draft', 'available')")
    )
    with op.batch_alter_table("project") as batch_op:
        batch_op.create_check_constraint(
            "ck_project_status",
            "status IN ('draft', 'available')",
        )


def downgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.drop_constraint("ck_project_status", type_="check")
