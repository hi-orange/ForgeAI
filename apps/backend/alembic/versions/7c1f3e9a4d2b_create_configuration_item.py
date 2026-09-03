"""create configuration item

Revision ID: 7c1f3e9a4d2b
Revises: 2a6c9e4f1b7d
Create Date: 2026-09-03 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7c1f3e9a4d2b"
down_revision: str | Sequence[str] | None = "8d2e4f6a8b0c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "configuration_item",
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
            comment="内部自增主键；对外请用 item_id",
        ),
        sa.Column(
            "item_id",
            sa.String(length=40),
            nullable=False,
            comment="对外稳定身份证，形如 ci_<uuid>；上游引用也用此值",
        ),
        sa.Column(
            "project_id",
            sa.Integer(),
            nullable=False,
            comment="所属项目 ID；删除项目时级联删除成果",
        ),
        sa.Column(
            "producer_run_id",
            sa.String(length=40),
            nullable=False,
            comment="产出该成果的 BuildRun.run_id；用于追溯与晚到判定",
        ),
        sa.Column(
            "semantic_type",
            sa.String(length=32),
            nullable=False,
            comment="成果类型：app_spec/system_design/code/test_report",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment="同项目同类型内从 1 递增的版本号",
        ),
        sa.Column(
            "schema_version",
            sa.Integer(),
            server_default="1",
            nullable=False,
            comment="payload 结构兼容版本",
        ),
        sa.Column(
            "payload",
            sa.JSON(),
            nullable=False,
            comment="正式成果正文 JSON；登记后不可原地修改",
        ),
        sa.Column(
            "content_hash",
            sa.String(length=64),
            nullable=False,
            comment="规范化 payload 的 SHA-256，用于内容比对",
        ),
        sa.Column(
            "upstream_item_ids",
            sa.JSON(),
            nullable=False,
            comment="直接上游成果的 item_id 列表",
        ),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default="usable",
            nullable=False,
            comment="usable=可作后续输入；unusable=仅审计不可再用",
        ),
        sa.Column(
            "unusable_reason",
            sa.String(length=500),
            nullable=True,
            comment="变为 unusable 时的原因；usable 时为空",
        ),
        sa.Column(
            "unusable_at",
            sa.DateTime(),
            nullable=True,
            comment="变为 unusable 的时间；usable 时为空",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
            comment="登记时间",
        ),
        sa.CheckConstraint(
            "semantic_type IN ('app_spec', 'system_design', 'code', 'test_report')",
            name="ck_configuration_item_semantic_type",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_configuration_item_positive_version",
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name="ck_configuration_item_positive_schema_version",
        ),
        sa.CheckConstraint(
            "state IN ('usable', 'unusable')",
            name="ck_configuration_item_state",
        ),
        sa.CheckConstraint(
            "((state = 'usable' AND unusable_reason IS NULL AND unusable_at IS NULL) "
            "OR (state = 'unusable' AND unusable_reason IS NOT NULL "
            "AND unusable_at IS NOT NULL))",
            name="ck_configuration_item_unusable_fields",
        ),
        sa.ForeignKeyConstraint(
            ["producer_run_id"],
            ["build_run.run_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", name="uq_configuration_item_item_id"),
        sa.UniqueConstraint(
            "project_id",
            "semantic_type",
            "version",
            name="uq_configuration_item_project_type_version",
        ),
        comment="项目正式成果版本（ConfigurationItem）",
    )
    op.create_index(
        op.f("ix_configuration_item_id"),
        "configuration_item",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_configuration_item_producer_run_id"),
        "configuration_item",
        ["producer_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_configuration_item_project_type_state",
        "configuration_item",
        ["project_id", "semantic_type", "state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_configuration_item_project_type_state",
        table_name="configuration_item",
    )
    op.drop_index(
        op.f("ix_configuration_item_producer_run_id"),
        table_name="configuration_item",
    )
    op.drop_index(op.f("ix_configuration_item_id"), table_name="configuration_item")
    op.drop_table("configuration_item")
