from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ConfigurationItemType(StrEnum):
    """ConfigurationManager 当前识别的四种正式成果。"""

    APP_SPEC = "app_spec"
    SYSTEM_DESIGN = "system_design"
    CODE = "code"
    TEST_REPORT = "test_report"


class ConfigurationItemState(StrEnum):
    """usable 可以作为后续输入；unusable 只能留作审计。"""

    USABLE = "usable"
    UNUSABLE = "unusable"


class ConfigurationItem(Base):
    """一个不可覆盖的正式成果版本。"""

    __tablename__ = "configuration_item"
    __table_args__ = (
        CheckConstraint(
            "semantic_type IN ('app_spec', 'system_design', 'code', 'test_report')",
            name="ck_configuration_item_semantic_type",
        ),
        CheckConstraint("version > 0", name="ck_configuration_item_positive_version"),
        CheckConstraint(
            "schema_version > 0",
            name="ck_configuration_item_positive_schema_version",
        ),
        CheckConstraint(
            "state IN ('usable', 'unusable')",
            name="ck_configuration_item_state",
        ),
        CheckConstraint(
            "((state = 'usable' AND unusable_reason IS NULL AND unusable_at IS NULL) "
            "OR (state = 'unusable' AND unusable_reason IS NOT NULL "
            "AND unusable_at IS NOT NULL))",
            name="ck_configuration_item_unusable_fields",
        ),
        UniqueConstraint("item_id", name="uq_configuration_item_item_id"),
        UniqueConstraint(
            "project_id",
            "semantic_type",
            "version",
            name="uq_configuration_item_project_type_version",
        ),
        Index(
            "ix_configuration_item_project_type_state",
            "project_id",
            "semantic_type",
            "state",
        ),
        {"comment": "项目正式成果版本（ConfigurationItem）"},
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
        autoincrement=True,
        comment="内部自增主键；对外请用 item_id",
    )
    item_id: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment="对外稳定身份证，形如 ci_<uuid>；上游引用也用此值",
    )
    project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属项目 ID；删除项目时级联删除成果",
    )
    producer_run_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("build_run.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="产出该成果的 BuildRun.run_id；用于追溯与晚到判定",
    )
    semantic_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="成果类型：app_spec/system_design/code/test_report",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="同项目同类型内从 1 递增的版本号",
    )
    schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="payload 结构兼容版本",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        comment="正式成果正文 JSON；登记后不可原地修改",
    )
    content_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="规范化 payload 的 SHA-256，用于内容比对",
    )
    upstream_item_ids: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        comment="直接上游成果的 item_id 列表",
    )
    state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ConfigurationItemState.USABLE.value,
        server_default=ConfigurationItemState.USABLE.value,
        comment="usable=可作后续输入；unusable=仅审计不可再用",
    )
    unusable_reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="变为 unusable 时的原因；usable 时为空",
    )
    unusable_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="变为 unusable 的时间；usable 时为空",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        nullable=False,
        comment="登记时间",
    )


_IMMUTABLE_FIELDS = (
    "id",
    "item_id",
    "project_id",
    "producer_run_id",
    "semantic_type",
    "version",
    "schema_version",
    "payload",
    "content_hash",
    "upstream_item_ids",
    "created_at",
)


@event.listens_for(ConfigurationItem, "before_update")
def _guard_configuration_item_update(
    _mapper: object,
    _connection: object,
    target: ConfigurationItem,
) -> None:
    """在 ORM UPDATE 发出前拦截非法变更。

    已登记成果的身份与正文（见 _IMMUTABLE_FIELDS）不可原地覆盖，需要新版本；
    允许的唯一生命周期变更是 usable → unusable，且必须同时写入原因与时间。
    """

    # inspect 拿到各字段相对加载时的变更历史；这里的 state 不是业务字段 target.state。
    state = sqlalchemy_inspect(target)
    changed_content = [
        field_name
        for field_name in _IMMUTABLE_FIELDS
        if state.attrs[field_name].history.has_changes()
    ]
    if changed_content:
        raise ValueError("已登记的 ConfigurationItem 内容不可修改；请创建新版本")

    # 生命周期三件套都没动则放行（内容已在上面拦过）。
    lifecycle_fields = ("state", "unusable_reason", "unusable_at")
    if not any(state.attrs[field_name].history.has_changes() for field_name in lifecycle_fields):
        return

    # history.deleted 是改之前的旧值；有变动则只许 usable→unusable 且原因、时间齐全。
    state_history = state.attrs.state.history
    previous_state = state_history.deleted[0] if state_history.deleted else None
    if (
        previous_state != ConfigurationItemState.USABLE.value
        or target.state != ConfigurationItemState.UNUSABLE.value
        or not target.unusable_reason
        or target.unusable_at is None
    ):
        raise ValueError("ConfigurationItem 状态只能从 usable 单向变为 unusable")
