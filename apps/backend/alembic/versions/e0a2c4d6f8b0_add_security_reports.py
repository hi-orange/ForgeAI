"""add immutable security reports and security workflow jobs

Revision ID: e0a2c4d6f8b0
Revises: d8f0a2b4c6e8
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e0a2c4d6f8b0"
down_revision: str | None = "d8f0a2b4c6e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WITH_SECURITY = (
    "job_type IN ("
    "'product_manager', 'architect', 'engineering', 'quality', 'security', 'deployment'"
    ")"
)
_WITHOUT_SECURITY = (
    "job_type IN ('product_manager', 'architect', 'engineering', 'quality', 'deployment')"
)


def upgrade() -> None:
    op.create_table(
        "security_report",
        sa.Column("report_id", sa.String(length=40), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("build_run_id", sa.String(length=40), nullable=False),
        sa.Column("code_item_id", sa.String(length=40), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("scanner_version", sa.String(length=64), nullable=False),
        sa.Column("conclusion", sa.String(length=16), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "conclusion IN ('passed', 'failed', 'blocked')",
            name="ck_security_report_conclusion",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["build_run_id"], ["build_run.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["code_item_id"],
            ["configuration_item.item_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "code_item_id",
            "scanner_version",
            name="uq_security_report_code_scanner",
        ),
    )
    op.create_index("ix_security_report_project_id", "security_report", ["project_id"])
    op.create_index("ix_security_report_build_run_id", "security_report", ["build_run_id"])
    with op.batch_alter_table("workflow_job") as batch_op:
        batch_op.drop_constraint("ck_workflow_job_type", type_="check")
        batch_op.create_check_constraint("ck_workflow_job_type", _WITH_SECURITY)


def downgrade() -> None:
    with op.batch_alter_table("workflow_job") as batch_op:
        batch_op.drop_constraint("ck_workflow_job_type", type_="check")
        batch_op.create_check_constraint("ck_workflow_job_type", _WITHOUT_SECURITY)
    op.drop_index("ix_security_report_build_run_id", table_name="security_report")
    op.drop_index("ix_security_report_project_id", table_name="security_report")
    op.drop_table("security_report")
