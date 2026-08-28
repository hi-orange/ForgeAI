"""create build run

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-27 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # active_slot 是跨数据库通用的“单项目单活跃任务”互斥槽：
    # queued/running 保存 1，succeeded/failed 保存 NULL；唯一约束允许保留多个终态历史。
    op.create_table(
        "build_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("active_slot", sa.SmallInteger(), server_default="1", nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_build_run_status",
        ),
        sa.CheckConstraint(
            "stage IS NULL OR stage IN ('pm', 'architect', 'developer', 'qa', 'runtime')",
            name="ck_build_run_stage",
        ),
        sa.CheckConstraint(
            "((status = 'queued' AND stage IS NULL) "
            "OR (status = 'running' AND stage IS NOT NULL) "
            "OR status IN ('succeeded', 'failed'))",
            name="ck_build_run_stage_by_status",
        ),
        sa.CheckConstraint(
            "((status IN ('queued', 'running') AND active_slot IS NOT NULL "
            "AND active_slot = 1) "
            "OR (status IN ('succeeded', 'failed') AND active_slot IS NULL))",
            name="ck_build_run_active_slot",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "active_slot", name="uq_build_run_project_active_slot"),
        sa.UniqueConstraint("run_id", name="uq_build_run_run_id"),
    )
    op.create_index(op.f("ix_build_run_id"), "build_run", ["id"], unique=False)
    op.create_index(
        "ix_build_run_project_status",
        "build_run",
        ["project_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_build_run_project_status", table_name="build_run")
    op.drop_index(op.f("ix_build_run_id"), table_name="build_run")
    op.drop_table("build_run")
