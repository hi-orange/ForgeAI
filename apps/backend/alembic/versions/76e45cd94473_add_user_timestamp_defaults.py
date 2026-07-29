"""add user timestamp defaults

Revision ID: 76e45cd94473
Revises: ecd5b1ddd3d9
Create Date: 2026-07-29 16:54:18.762451

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "76e45cd94473"
down_revision: str | Sequence[str] | None = "ecd5b1ddd3d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE user "
        "MODIFY created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "MODIFY updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP "
        "ON UPDATE CURRENT_TIMESTAMP"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE user MODIFY created_at DATETIME NOT NULL, MODIFY updated_at DATETIME NOT NULL"
    )
