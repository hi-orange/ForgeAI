"""Retire unused inline project payloads and align persisted column descriptions.

Revision ID: b5d7f9a1c3e5
Revises: a4b6c8d0e2f4

The old inline payloads are no longer read by the application. Their contents are
discarded; downgrade restores columns only. Current versioned artifacts are kept.
"""

import sqlalchemy as sa

from alembic import op

revision = "b5d7f9a1c3e5"
down_revision = "a4b6c8d0e2f4"
branch_labels = None
depends_on = None

LEGACY_COLUMNS = {
    "prd": sa.Text(),
    "approved_spec": sa.Text(),
    "approved_at": sa.DateTime(),
    "generated_files": sa.Text(),
    "build_error": sa.Text(),
    "built_at": sa.DateTime(),
    "validation_report": sa.Text(),
    "website_revision": sa.Integer(),
}

COMMENTS = {
    ("build_run", "status"): (
        "queued/running/succeeded/failed",
        "queued/running/succeeded/failed/cancelled",
    ),
    ("build_run", "paused_at"): (None, "用户暂停构建的时间；NULL 表示可被 Worker 领取"),
    ("configuration_item", "semantic_type"): (
        "成果类型：app_spec/system_design/code/test_report",
        "成果类型：app_spec/system_design/acceptance_test_plan/code/test_report",
    ),
    ("project_message_classification", "category"): (
        "inquiry/stop/product_change/implementation_repair",
        "inquiry=询问不改行为；stop=停止构建；product_change=产品变更；implementation_repair=修已有实现",
    ),
    ("task_artifact", "id"): (None, "内部主键"),
    ("task_artifact", "task_id"): (None, "所属任务的稳定标识"),
    ("task_artifact", "configuration_item_id"): (None, "附属产物的 ConfigurationItem 编号"),
    ("task_artifact", "artifact_role"): (None, "附属角色：system_design / test_report / other"),
    ("task_artifact", "created_at"): (None, "登记时间"),
}


def _comments(upgrade: bool) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return  # SQLite does not persist column comments.
    inspector = sa.inspect(bind)
    for (table, name), (old, new) in COMMENTS.items():
        column = next(col for col in inspector.get_columns(table) if col["name"] == name)
        op.alter_column(
            table,
            name,
            comment=new if upgrade else old,
            existing_type=column["type"],
            existing_nullable=column["nullable"],
            existing_server_default=sa.text(column["default"]) if column.get("default") else None,
            autoincrement=True if (table, name) == ("task_artifact", "id") else None,
        )


def upgrade() -> None:
    # MySQL DDL commits individually. Resume safely after a partial upgrade.
    existing = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("project")}
    with op.batch_alter_table("project") as batch:
        for name in LEGACY_COLUMNS:
            if name in existing:
                batch.drop_column(name)
    _comments(True)


def downgrade() -> None:
    with op.batch_alter_table("project") as batch:
        for name, type_ in LEGACY_COLUMNS.items():
            batch.add_column(
                sa.Column(
                    name,
                    type_,
                    nullable=name != "website_revision",
                    server_default="0" if name == "website_revision" else None,
                )
            )
    _comments(False)
