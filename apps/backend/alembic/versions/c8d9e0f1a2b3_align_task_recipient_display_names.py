"""align task recipient with role display names

Revision ID: c8d9e0f1a2b3
Revises: 6f1a2b3c4d5e
Create Date: 2026-09-18 14:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8d9e0f1a2b3"
down_revision: str | Sequence[str] | None = "6f1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FORWARD = (
    ("ProductManager", "Product Manager"),
    ("SolutionArchitect", "Architect"),
    ("SoftwareEngineer", "Code Engineer"),
    ("QAEngineer", "Test Engineer"),
)


def upgrade() -> None:
    with op.batch_alter_table("task") as batch_op:
        batch_op.drop_constraint("ck_task_recipient", type_="check")
    for old, new in _FORWARD:
        op.execute(
            sa.text("UPDATE task SET recipient = :new WHERE recipient = :old").bindparams(
                old=old, new=new
            )
        )
    with op.batch_alter_table("task") as batch_op:
        batch_op.create_check_constraint(
            "ck_task_recipient",
            "recipient IN ('Product Manager', 'Architect', 'Code Engineer', 'Test Engineer')",
        )


def downgrade() -> None:
    with op.batch_alter_table("task") as batch_op:
        batch_op.drop_constraint("ck_task_recipient", type_="check")
    for old, new in _FORWARD:
        op.execute(
            sa.text("UPDATE task SET recipient = :old WHERE recipient = :new").bindparams(
                old=old, new=new
            )
        )
    with op.batch_alter_table("task") as batch_op:
        batch_op.create_check_constraint(
            "ck_task_recipient",
            "recipient IN ('ProductManager', 'SolutionArchitect', "
            "'SoftwareEngineer', 'QAEngineer')",
        )
