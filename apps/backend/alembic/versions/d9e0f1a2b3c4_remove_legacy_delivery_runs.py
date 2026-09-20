"""remove legacy delivery runs that bypass Architect or user approval

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-09-20 16:00:00.000000

"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: str | Sequence[str] | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPROVAL_VERSION = "requirements_approval_v1"


def _ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    return []


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    build_run = sa.Table("build_run", metadata, autoload_with=bind)
    plan = sa.Table("plan", metadata, autoload_with=bind)
    task = sa.Table("task", metadata, autoload_with=bind)
    item = sa.Table("configuration_item", metadata, autoload_with=bind)
    result = sa.Table("task_result", metadata, autoload_with=bind)

    plan_runs = dict(bind.execute(sa.select(plan.c.plan_id, plan.c.build_run_id)).all())
    item_types = dict(bind.execute(sa.select(item.c.item_id, item.c.semantic_type)).all())
    result_versions = {
        row.configuration_item_id: row.prompt_version
        for row in bind.execute(sa.select(result.c.configuration_item_id, result.c.prompt_version))
    }

    obsolete_run_ids: set[str] = set()
    rows = bind.execute(
        sa.select(
            task.c.plan_id,
            task.c.task_key,
            task.c.recipient,
            task.c.expected_output_type,
            task.c.input_configuration_item_ids,
        )
    )
    for row in rows:
        input_ids = _ids(row.input_configuration_item_ids)
        if len(input_ids) != 1:
            continue
        input_id = input_ids[0]
        direct_code = (
            row.task_key == "engineering_delivery"
            and row.recipient == "Code Engineer"
            and row.expected_output_type == "code"
            and item_types.get(input_id) == "app_spec"
        )
        preapproval_design = (
            row.task_key == "system_design"
            and row.recipient == "Architect"
            and row.expected_output_type == "system_design"
            and item_types.get(input_id) == "app_spec"
            and result_versions.get(input_id) != _APPROVAL_VERSION
        )
        if direct_code or preapproval_design:
            run_id = plan_runs.get(row.plan_id)
            if run_id:
                obsolete_run_ids.add(str(run_id))

    if obsolete_run_ids:
        # BuildRun owns Plans and produced ConfigurationItems; database cascades remove the
        # obsolete task ledger, executions, results and downstream artifacts atomically.
        bind.execute(sa.delete(build_run).where(build_run.c.run_id.in_(obsolete_run_ids)))


def downgrade() -> None:
    # Deleted legacy execution history cannot be reconstructed safely.
    pass
