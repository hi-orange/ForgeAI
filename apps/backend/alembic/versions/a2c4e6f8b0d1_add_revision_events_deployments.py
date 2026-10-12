"""add revision lineage, decision events, deployment records and cancellation

Revision ID: a2c4e6f8b0d1
Revises: 1a2b3c4d5e6f
Create Date: 2026-10-10 10:00:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "a2c4e6f8b0d1"
down_revision: str | Sequence[str] | None = "1a2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _create_table_if_missing(table_name: str, *elements: Any) -> None:
    """Recover safely after partially applied MySQL DDL.

    MySQL commits each DDL statement independently. A later failure therefore leaves
    complete tables behind even though Alembic cannot advance its version row.
    """

    if not _has_table(table_name):
        op.create_table(table_name, *elements)


def _create_index_if_missing(index_name: str, table_name: str, columns: list[str]) -> None:
    if not _has_table(table_name):
        return
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table_name)}
    if index_name not in indexes:
        op.create_index(index_name, table_name, columns)


def _replace_check_constraint(table_name: str, name: str, condition: str) -> None:
    checks = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if item.get("name")
    }
    if name in checks:
        op.drop_constraint(name, table_name, type_="check")
    op.create_check_constraint(name, table_name, condition)


def _drop_table_if_exists(table_name: str) -> None:
    if _has_table(table_name):
        op.drop_table(table_name)


def upgrade() -> None:
    _replace_check_constraint(
        "build_run",
        "ck_build_run_status",
        "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
    )
    _replace_check_constraint(
        "build_run",
        "ck_build_run_stage_by_status",
        "((status = 'queued' AND stage IS NULL) OR "
        "(status = 'running' AND stage IS NOT NULL) OR "
        "status IN ('succeeded', 'failed', 'cancelled'))",
    )
    _replace_check_constraint(
        "build_run",
        "ck_build_run_active_slot",
        "((status IN ('queued', 'running') AND active_slot = 1) OR "
        "(status IN ('succeeded', 'failed', 'cancelled') AND active_slot IS NULL))",
    )
    _replace_check_constraint(
        "task_execution",
        "ck_task_execution_status",
        "(status = 'running' AND active_slot = 1) OR "
        "(status IN ('failed', 'succeeded', 'superseded', 'cancelled') AND active_slot IS NULL)",
    )

    _create_table_if_missing(
        "run_revision",
        sa.Column("revision_id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("source_run_id", sa.String(40), nullable=False),
        sa.Column("target_run_id", sa.String(40), nullable=False),
        sa.Column("cause_message_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("baseline_app_spec_item_id", sa.String(40), nullable=False),
        sa.Column("baseline_code_item_id", sa.String(40)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('product_change', 'implementation_repair')", name="ck_run_revision_kind"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_run_id"], ["build_run.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_run_id"], ["build_run.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cause_message_id"], ["project_message.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["baseline_app_spec_item_id"], ["configuration_item.item_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["baseline_code_item_id"], ["configuration_item.item_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("target_run_id", name="uq_run_revision_target_run"),
        sa.UniqueConstraint("cause_message_id", name="uq_run_revision_cause_message"),
    )
    _create_index_if_missing("ix_run_revision_project_id", "run_revision", ["project_id"])

    _create_table_if_missing(
        "agent_decision_event",
        sa.Column("event_id", sa.String(40), primary_key=True),
        sa.Column("decision_key", sa.String(200), nullable=False, unique=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("build_run_id", sa.String(40)),
        sa.Column("message_id", sa.Integer()),
        sa.Column("report_item_id", sa.String(40)),
        sa.Column("category", sa.String(40)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("decided_by", sa.String(16), nullable=False),
        sa.Column("recipient", sa.String(32)),
        sa.Column("source_artifact_ids", sa.JSON(), nullable=False),
        sa.Column("required_inputs", sa.JSON(), nullable=False),
        sa.Column("risk_flags", sa.JSON(), nullable=False),
        sa.Column("decision_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("applied_at", sa.DateTime()),
        sa.CheckConstraint(
            "status IN ('decided', 'applied', 'failed')", name="ck_agent_decision_event_status"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["build_run_id"], ["build_run.run_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["message_id"], ["project_message.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["report_item_id"], ["configuration_item.item_id"], ondelete="SET NULL"
        ),
    )
    _create_index_if_missing(
        "ix_agent_decision_event_project_id", "agent_decision_event", ["project_id"]
    )
    _create_index_if_missing(
        "ix_agent_decision_event_build_run_id", "agent_decision_event", ["build_run_id"]
    )
    _create_index_if_missing(
        "ix_agent_decision_event_message_id", "agent_decision_event", ["message_id"]
    )

    _create_table_if_missing(
        "agent_usage_event",
        sa.Column("event_id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("build_run_id", sa.String(40)),
        sa.Column("task_id", sa.String(40)),
        sa.Column("execution_id", sa.String(40)),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("model", sa.String(100)),
        sa.Column("protocol", sa.String(16), nullable=False),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_microusd", sa.Integer()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["build_run_id"], ["build_run.run_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["task.task_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["task_execution.execution_id"], ondelete="SET NULL"
        ),
    )
    _create_index_if_missing("ix_agent_usage_event_project_id", "agent_usage_event", ["project_id"])
    _create_index_if_missing(
        "ix_agent_usage_event_build_run_id", "agent_usage_event", ["build_run_id"]
    )
    _create_index_if_missing("ix_agent_usage_event_task_id", "agent_usage_event", ["task_id"])

    _create_table_if_missing(
        "agent_eval_label",
        sa.Column("label_id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("expected_category", sa.String(40), nullable=False),
        sa.Column("expected_action", sa.String(64)),
        sa.Column("labeled_by_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["project_message.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["labeled_by_user_id"], ["user.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("message_id", name="uq_agent_eval_label_message"),
    )
    _create_index_if_missing("ix_agent_eval_label_project_id", "agent_eval_label", ["project_id"])

    _create_table_if_missing(
        "deployment",
        sa.Column("deployment_id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("build_run_id", sa.String(40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("url", sa.String(500)),
        sa.Column("domain", sa.String(255)),
        sa.Column("image_reference", sa.String(500)),
        sa.Column("secret_reference", sa.String(200)),
        sa.Column("compose_project", sa.String(100), nullable=False, unique=True),
        sa.Column("port", sa.Integer()),
        sa.Column("previous_deployment_id", sa.String(40)),
        sa.Column("logs", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("deployed_at", sa.DateTime()),
        sa.CheckConstraint(
            "status IN ('queued', 'building', 'ready', 'failed', 'stopped', 'rolled_back')",
            name="ck_deployment_status",
        ),
        sa.CheckConstraint("revision > 0", name="ck_deployment_positive_revision"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["build_run_id"], ["build_run.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["previous_deployment_id"], ["deployment.deployment_id"], ondelete="SET NULL"
        ),
    )
    _create_index_if_missing("ix_deployment_project_id", "deployment", ["project_id"])


def downgrade() -> None:
    _drop_table_if_exists("deployment")
    _drop_table_if_exists("agent_eval_label")
    _drop_table_if_exists("agent_usage_event")
    _drop_table_if_exists("agent_decision_event")
    _drop_table_if_exists("run_revision")
    _replace_check_constraint(
        "task_execution",
        "ck_task_execution_status",
        "(status = 'running' AND active_slot = 1) OR "
        "(status IN ('failed', 'succeeded', 'superseded') AND active_slot IS NULL)",
    )
    _replace_check_constraint(
        "build_run",
        "ck_build_run_status",
        "status IN ('queued', 'running', 'succeeded', 'failed')",
    )
    _replace_check_constraint(
        "build_run",
        "ck_build_run_stage_by_status",
        "((status = 'queued' AND stage IS NULL) OR "
        "(status = 'running' AND stage IS NOT NULL) OR status IN ('succeeded', 'failed'))",
    )
    _replace_check_constraint(
        "build_run",
        "ck_build_run_active_slot",
        "((status IN ('queued', 'running') AND active_slot = 1) OR "
        "(status IN ('succeeded', 'failed') AND active_slot IS NULL))",
    )
