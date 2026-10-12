"""add durable workflow jobs and append-only events

Revision ID: b4d6f8a0c2e4
Revises: a2c4e6f8b0d1
Create Date: 2026-10-12 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b4d6f8a0c2e4"
down_revision: str | Sequence[str] | None = "a2c4e6f8b0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_job",
        sa.Column("job_id", sa.String(40), primary_key=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("job_type", sa.String(24), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("build_run_id", sa.String(40)),
        sa.Column("task_id", sa.String(40)),
        sa.Column("execution_id", sa.String(40)),
        sa.Column("deployment_id", sa.String(40)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("lease_owner", sa.String(120)),
        sa.Column("lease_expires_at", sa.DateTime()),
        sa.Column("last_heartbeat_at", sa.DateTime()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "job_type IN ('architect', 'engineering', 'quality', 'deployment')",
            name="ck_workflow_job_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled')",
            name="ck_workflow_job_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_workflow_job_attempt"),
        sa.CheckConstraint("max_attempts > 0", name="ck_workflow_job_max_attempts"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["build_run_id"], ["build_run.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["task.task_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["task_execution.execution_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["deployment.deployment_id"], ondelete="CASCADE"
        ),
    )
    for name, columns in (
        ("ix_workflow_job_project_id", ["project_id"]),
        ("ix_workflow_job_build_run_id", ["build_run_id"]),
        ("ix_workflow_job_task_id", ["task_id"]),
        ("ix_workflow_job_execution_id", ["execution_id"]),
        ("ix_workflow_job_deployment_id", ["deployment_id"]),
        ("ix_workflow_job_available_at", ["available_at"]),
        ("ix_workflow_job_lease_expires_at", ["lease_expires_at"]),
    ):
        op.create_index(name, "workflow_job", columns)

    op.create_table(
        "workflow_event",
        sa.Column("event_id", sa.String(40), primary_key=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("stream_key", sa.String(120), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("build_run_id", sa.String(40)),
        sa.Column("task_id", sa.String(40)),
        sa.Column("execution_id", sa.String(40)),
        sa.Column("job_id", sa.String(40)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["build_run_id"], ["build_run.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["task.task_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["task_execution.execution_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["workflow_job.job_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("stream_key", "sequence", name="uq_workflow_event_stream_sequence"),
    )
    for name, columns in (
        ("ix_workflow_event_stream_key", ["stream_key"]),
        ("ix_workflow_event_event_type", ["event_type"]),
        ("ix_workflow_event_project_id", ["project_id"]),
        ("ix_workflow_event_build_run_id", ["build_run_id"]),
        ("ix_workflow_event_task_id", ["task_id"]),
        ("ix_workflow_event_execution_id", ["execution_id"]),
        ("ix_workflow_event_job_id", ["job_id"]),
        ("ix_workflow_event_created_at", ["created_at"]),
    ):
        op.create_index(name, "workflow_event", columns)


def downgrade() -> None:
    op.drop_table("workflow_event")
    op.drop_table("workflow_job")
