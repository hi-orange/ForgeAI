"""add immutable acceptance test plan

Revision ID: 1a2b3c4d5e6f
Revises: f1b2c3d4e5f6
Create Date: 2026-10-08 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "1a2b3c4d5e6f"
down_revision: str | Sequence[str] | None = "f1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("configuration_item") as batch_op:
        batch_op.drop_constraint("ck_configuration_item_semantic_type", type_="check")
        batch_op.create_check_constraint(
            "ck_configuration_item_semantic_type",
            "semantic_type IN ('app_spec', 'system_design', 'acceptance_test_plan', "
            "'code', 'test_report')",
        )


def downgrade() -> None:
    op.execute("DELETE FROM configuration_item WHERE semantic_type = 'acceptance_test_plan'")
    with op.batch_alter_table("configuration_item") as batch_op:
        batch_op.drop_constraint("ck_configuration_item_semantic_type", type_="check")
        batch_op.create_check_constraint(
            "ck_configuration_item_semantic_type",
            "semantic_type IN ('app_spec', 'system_design', 'code', 'test_report')",
        )
