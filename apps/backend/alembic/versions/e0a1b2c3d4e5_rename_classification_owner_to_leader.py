"""rename project message classification owner to Leader

Revision ID: e0a1b2c3d4e5
Revises: d9e0f1a2b3c4
Create Date: 2026-09-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e0a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "d9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_comment(comment: str) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "mysql":
        escaped = comment.replace("'", "''")
        op.execute(sa.text(f"ALTER TABLE `project_message_classification` COMMENT = '{escaped}'"))
    elif dialect == "postgresql":
        op.execute(
            sa.text("COMMENT ON TABLE project_message_classification IS :comment").bindparams(
                comment=comment
            )
        )


def upgrade() -> None:
    _set_comment("用户消息的 Leader 分类结果")


def downgrade() -> None:
    _set_comment("用户消息的 Manager 分类结果")
