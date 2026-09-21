"""repair plans whose completed tasks were committed before plan derivation

Revision ID: f1b2c3d4e5f6
Revises: e0a1b2c3d4e5
Create Date: 2026-09-21 00:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "e0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE plan "
            "SET status = 'succeeded' "
            "WHERE status IN ('pending', 'running') "
            "AND EXISTS ("
            "  SELECT 1 FROM task WHERE task.plan_id = plan.plan_id"
            ") "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM task "
            "  WHERE task.plan_id = plan.plan_id AND task.status <> 'succeeded'"
            ")"
        )
    )


def downgrade() -> None:
    # The previous states cannot be reconstructed safely. Keeping the derived
    # succeeded status preserves the already-completed task ledger.
    pass
