"""Deterministic delivery units from an approved app_spec."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import ConflictException
from app.schemas.app_spec import AppSpec


class WorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    requirement_ids: list[str] = Field(min_length=1)
    title: str
    acceptance: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    status: str = "pending"
    summary: str | None = None


def plan_delivery(spec: AppSpec) -> list[WorkItem]:
    """One work item per approved feature. Does not invent extra product scope."""
    items: list[WorkItem] = []
    for feature in spec.features:
        acceptance = [
            criterion.text
            for criterion in spec.acceptance_criteria
            if feature.id in criterion.source_ids
        ]
        related_data = [item.text for item in spec.data_requirements]
        related_ui = [item.text for item in spec.interface_requirements]
        deliverables = [
            "必要时新增 SQLAlchemy 模型与 Alembic 迁移",
            "在 backend/app/api 增加真实 FastAPI 接口并接入 router",
            "在 frontend 提供对应该功能的页面或组件并调用真实接口",
        ]
        if related_data:
            deliverables.append("覆盖相关数据：" + "；".join(related_data[:6]))
        if related_ui:
            deliverables.append("覆盖相关界面：" + "；".join(related_ui[:6]))
        items.append(
            WorkItem(
                id=feature.id,
                requirement_ids=[feature.id],
                title=feature.text,
                acceptance=acceptance,
                deliverables=deliverables,
            )
        )
    validate_delivery_plan(spec, items)
    return items


def validate_delivery_plan(spec: AppSpec, items: list[WorkItem]) -> None:
    covered = [req_id for item in items for req_id in item.requirement_ids]
    feature_ids = [feature.id for feature in spec.features]
    if not feature_ids:
        raise ConflictException("获批需求没有可实现的功能")
    if sorted(covered) != sorted(feature_ids):
        raise ConflictException("交付计划必须覆盖且仅覆盖获批功能")
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise ConflictException("工作单元 id 重复")


def work_items_to_json(items: list[WorkItem]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items]
