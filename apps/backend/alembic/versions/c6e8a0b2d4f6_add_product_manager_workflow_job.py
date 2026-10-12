"""allow Product Manager recovery jobs

Revision ID: c6e8a0b2d4f6
Revises: b4d6f8a0c2e4
Create Date: 2026-10-12 13:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c6e8a0b2d4f6"
down_revision: str | Sequence[str] | None = "b4d6f8a0c2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WITH_PRODUCT_MANAGER = (
    "job_type IN ('product_manager', 'architect', 'engineering', 'quality', 'deployment')"
)
_WITHOUT_PRODUCT_MANAGER = "job_type IN ('architect', 'engineering', 'quality', 'deployment')"


def upgrade() -> None:
    with op.batch_alter_table("workflow_job") as batch_op:
        batch_op.drop_constraint("ck_workflow_job_type", type_="check")
        batch_op.create_check_constraint("ck_workflow_job_type", _WITH_PRODUCT_MANAGER)


def downgrade() -> None:
    with op.batch_alter_table("workflow_job") as batch_op:
        batch_op.drop_constraint("ck_workflow_job_type", type_="check")
        batch_op.create_check_constraint("ck_workflow_job_type", _WITHOUT_PRODUCT_MANAGER)
