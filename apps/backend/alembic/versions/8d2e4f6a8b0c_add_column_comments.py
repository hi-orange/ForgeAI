"""add column comments for existing tables

Revision ID: 8d2e4f6a8b0c
Revises: 2a6c9e4f1b7d
Create Date: 2026-09-03 15:55:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8d2e4f6a8b0c"
down_revision: str | Sequence[str] | None = "2a6c9e4f1b7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE `user` COMMENT = '平台登录用户'"))
    op.alter_column(
        "user",
        "id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment="内部自增主键",
        autoincrement=True,
    )
    op.alter_column(
        "user",
        "username",
        existing_type=sa.String(length=100),
        existing_nullable=False,
        comment="登录用户名，唯一",
    )
    op.alter_column(
        "user",
        "hashed_password",
        existing_type=sa.String(length=255),
        existing_nullable=False,
        comment="密码哈希，不明文存储",
    )
    op.alter_column(
        "user",
        "email",
        existing_type=sa.String(length=100),
        existing_nullable=False,
        comment="邮箱，唯一",
    )
    op.alter_column(
        "user",
        "avatar",
        existing_type=sa.String(length=255),
        existing_nullable=True,
        comment="头像 URL，可空",
    )
    op.alter_column(
        "user",
        "created_at",
        existing_type=sa.DateTime(),
        existing_nullable=False,
        existing_server_default=sa.text("CURRENT_TIMESTAMP"),
        comment="创建时间",
    )
    op.alter_column(
        "user",
        "updated_at",
        existing_type=sa.DateTime(),
        existing_nullable=False,
        existing_server_default=sa.text("CURRENT_TIMESTAMP"),
        comment="最后更新时间",
    )

    op.execute(sa.text("ALTER TABLE `project` COMMENT = '用户拥有的应用项目'"))
    op.alter_column(
        "project",
        "id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment="内部自增主键",
        autoincrement=True,
    )
    op.alter_column(
        "project",
        "user_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment="所属用户 ID",
    )
    op.alter_column(
        "project",
        "name",
        existing_type=sa.String(length=200),
        existing_nullable=False,
        comment="项目名称",
    )
    op.alter_column(
        "project",
        "description",
        existing_type=sa.Text(),
        existing_nullable=True,
        comment="项目描述，可空",
    )
    op.alter_column(
        "project",
        "prompt",
        existing_type=sa.Text(),
        existing_nullable=True,
        comment="创建时的初始需求文本",
    )
    op.alter_column(
        "project",
        "status",
        existing_type=sa.String(length=32),
        existing_nullable=False,
        existing_server_default=sa.text("'draft'"),
        comment="draft=尚无可用版本；available=已有可用版本",
    )
    op.alter_column(
        "project",
        "created_at",
        existing_type=sa.DateTime(),
        existing_nullable=False,
        existing_server_default=sa.text("CURRENT_TIMESTAMP"),
        comment="创建时间",
    )
    op.alter_column(
        "project",
        "updated_at",
        existing_type=sa.DateTime(),
        existing_nullable=False,
        existing_server_default=sa.text("CURRENT_TIMESTAMP"),
        comment="最后更新时间",
    )

    op.execute(sa.text("ALTER TABLE `build_run` COMMENT = '一次构建任务工单'"))
    op.alter_column(
        "build_run",
        "id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment="内部自增主键；对外请用 run_id",
        autoincrement=True,
    )
    op.alter_column(
        "build_run",
        "project_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment="所属项目 ID",
    )
    op.alter_column(
        "build_run",
        "run_id",
        existing_type=sa.String(length=40),
        existing_nullable=False,
        comment="对外任务标识，形如 run_<uuid>",
    )
    op.alter_column(
        "build_run",
        "status",
        existing_type=sa.String(length=32),
        existing_nullable=False,
        existing_server_default=sa.text("'queued'"),
        comment="queued/running/succeeded/failed",
    )
    op.alter_column(
        "build_run",
        "stage",
        existing_type=sa.String(length=32),
        existing_nullable=True,
        comment="running 时的当前阶段；queued 时为空",
    )
    op.alter_column(
        "build_run",
        "error",
        existing_type=sa.Text(),
        existing_nullable=True,
        comment="失败时的错误信息",
    )
    op.alter_column(
        "build_run",
        "active_slot",
        existing_type=sa.SmallInteger(),
        existing_nullable=True,
        existing_server_default=sa.text("'1'"),
        comment="占用项目构建名额时为 1；终态为 NULL",
    )
    op.alter_column(
        "build_run",
        "created_at",
        existing_type=sa.DateTime(),
        existing_nullable=False,
        existing_server_default=sa.text("CURRENT_TIMESTAMP"),
        comment="创建时间",
    )
    op.alter_column(
        "build_run",
        "updated_at",
        existing_type=sa.DateTime(),
        existing_nullable=False,
        existing_server_default=sa.text("CURRENT_TIMESTAMP"),
        comment="最后更新时间",
    )

    op.execute(sa.text("ALTER TABLE `project_message` COMMENT = '项目内用户可见的对话消息'"))
    op.alter_column(
        "project_message",
        "id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment="内部自增主键",
        autoincrement=True,
    )
    op.alter_column(
        "project_message",
        "project_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment="所属项目 ID",
    )
    op.alter_column(
        "project_message",
        "sequence",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment="项目内从 1 递增的对话序号，用于排序与增量拉取",
    )
    op.alter_column(
        "project_message",
        "sender",
        existing_type=sa.String(length=16),
        existing_nullable=False,
        comment="发送方：user 或 assistant",
    )
    op.alter_column(
        "project_message",
        "content",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment="消息正文",
    )
    op.alter_column(
        "project_message",
        "client_message_id",
        existing_type=sa.String(length=100),
        existing_nullable=False,
        comment="前端幂等键；同项目内唯一，防止重试重复写入",
    )
    op.alter_column(
        "project_message",
        "created_at",
        existing_type=sa.DateTime(),
        existing_nullable=False,
        existing_server_default=sa.text("CURRENT_TIMESTAMP"),
        comment="创建时间",
    )

    op.execute(
        sa.text(
            "ALTER TABLE `project_message_classification` "
            "COMMENT = '用户消息的 ProjectManager 分类结果'"
        )
    )
    op.alter_column(
        "project_message_classification",
        "id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment="内部自增主键",
        autoincrement=True,
    )
    op.alter_column(
        "project_message_classification",
        "message_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment="对应的项目消息 ID；一条消息最多一条分类",
    )
    op.alter_column(
        "project_message_classification",
        "category",
        existing_type=sa.String(length=32),
        existing_nullable=False,
        comment="inquiry/stop/product_change/implementation_repair",
    )
    op.alter_column(
        "project_message_classification",
        "decision_summary",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment="一句可审计的分类依据",
    )
    op.alter_column(
        "project_message_classification",
        "classifier_model",
        existing_type=sa.String(length=100),
        existing_nullable=False,
        comment="当时使用的模型名",
    )
    op.alter_column(
        "project_message_classification",
        "prompt_version",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        comment="当时使用的 prompt 版本标识",
    )
    op.alter_column(
        "project_message_classification",
        "created_at",
        existing_type=sa.DateTime(),
        existing_nullable=False,
        existing_server_default=sa.text("CURRENT_TIMESTAMP"),
        comment="分类时间",
    )


def downgrade() -> None:
    for table, columns in (
        (
            "project_message_classification",
            (
                "created_at",
                "prompt_version",
                "classifier_model",
                "decision_summary",
                "category",
                "message_id",
                "id",
            ),
        ),
        (
            "project_message",
            (
                "created_at",
                "client_message_id",
                "content",
                "sender",
                "sequence",
                "project_id",
                "id",
            ),
        ),
        (
            "build_run",
            (
                "updated_at",
                "created_at",
                "active_slot",
                "error",
                "stage",
                "status",
                "run_id",
                "project_id",
                "id",
            ),
        ),
        (
            "project",
            (
                "updated_at",
                "created_at",
                "status",
                "prompt",
                "description",
                "name",
                "user_id",
                "id",
            ),
        ),
        (
            "user",
            (
                "updated_at",
                "created_at",
                "avatar",
                "email",
                "hashed_password",
                "username",
                "id",
            ),
        ),
    ):
        for column in columns:
            op.alter_column(table, column, comment=None)

    for table in (
        "project_message_classification",
        "project_message",
        "build_run",
        "project",
        "user",
    ):
        op.execute(sa.text(f"ALTER TABLE `{table}` COMMENT = ''"))
