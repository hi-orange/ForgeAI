"""Deterministic delivery units from an approved app_spec."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import ConflictException
from app.schemas.app_spec import AppSpec
from app.schemas.test_report import TestReport, VerificationStatus

# Soft cap for one work-item file plan. Planners may overshoot; the platform
# truncates in dependency order rather than failing the whole engineering turn.
MAX_FILES_PER_WORK_ITEM = 20


class WorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    requirement_ids: list[str] = Field(min_length=1)
    title: str
    acceptance: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    status: str = "pending"
    summary: str | None = None


class FileTask(BaseModel):
    """One ordered source-file change in a platform-driven implementation plan."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    path: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=2000)
    context_paths: list[str] = Field(default_factory=list, max_length=12)
    status: Literal["pending", "completed"] = "pending"
    content_hash: str | None = None


class ImplementationPlan(BaseModel):
    """Executable file graph for one work item, not a semantic product artifact."""

    model_config = ConfigDict(extra="forbid")

    work_item_id: str
    summary: str = Field(min_length=1, max_length=2000)
    # One work item is a bounded vertical slice. Very large plans make one model
    # rewrite unrelated files and turn a single check failure into dozens of edits.
    files: list[FileTask] = Field(min_length=1, max_length=MAX_FILES_PER_WORK_ITEM)

    def validate_unique_paths(self) -> None:
        ids = [item.id for item in self.files]
        if len(ids) != len(set(ids)):
            raise ConflictException("文件级实施计划包含重复任务 id")
        paths = [item.path.replace("\\", "/") for item in self.files]
        if len(paths) != len(set(paths)):
            raise ConflictException("文件级实施计划包含重复目标路径")


def plan_delivery(spec: AppSpec) -> list[WorkItem]:
    """One work item per approved feature. Does not invent extra product scope."""
    items: list[WorkItem] = []
    for index, feature in enumerate(spec.features):
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
            "保留此前已完成的获批功能，不重写、删除或改名其公共契约",
        ]
        if index == 0 and related_data:
            deliverables.append("覆盖相关数据：" + "；".join(related_data[:6]))
        if index == 0 and related_ui:
            deliverables.append("覆盖相关界面：" + "；".join(related_ui[:6]))
        if index > 0:
            deliverables.append("复用第一个纵向切片已经建立的数据、路由、布局和客户端基础")
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


def plan_quality_repair(spec: AppSpec, report: TestReport) -> list[WorkItem]:
    """Turn independent QA evidence into small, executable repair units.

    A repair plan does not replay the approved feature list. It carries only the
    failed observations back to Code Engineer while retaining requirement ids for
    traceability.
    """

    feature_ids = {item.id for item in spec.features}
    acceptance_sources = {
        item.id: [source_id for source_id in item.source_ids if source_id in feature_ids]
        for item in spec.acceptance_criteria
    }
    failed_ids = list(
        dict.fromkeys(
            source_id
            for item in report.requirement_results
            if item.status != VerificationStatus.PASSED
            for source_id in acceptance_sources.get(item.requirement_id, [])
        )
    )
    fallback_ids = failed_ids or [item.id for item in spec.features]
    items: list[WorkItem] = []
    for defect in report.defects[:20]:
        related = list(
            dict.fromkeys(
                source_id
                for requirement_id in defect.related_requirement_ids
                for source_id in acceptance_sources.get(requirement_id, [])
            )
        )
        items.append(
            WorkItem(
                id=f"repair_{defect.defect_id}",
                requirement_ids=related or fallback_ids,
                title=f"修复验收缺陷：{defect.title}",
                acceptance=[defect.description, defect.evidence],
                deliverables=[
                    "只修改解决该缺陷所需的最小文件集合",
                    "保留已经通过验收的功能与公共契约",
                    "完成后重新运行平台检查并交给独立验收",
                ],
            )
        )
    if not items:
        failed_evidence = [
            item.evidence
            for item in report.requirement_results
            if item.status != VerificationStatus.PASSED
        ] + [
            item.evidence
            for item in report.check_results
            if item.status != VerificationStatus.PASSED
        ]
        items.append(
            WorkItem(
                id="repair_quality_report",
                requirement_ids=fallback_ids,
                title="根据独立验收结果修复阻断项",
                acceptance=failed_evidence[:12] or [report.summary],
                deliverables=[
                    "定位验收报告中的真实失败原因并进行最小修复",
                    "不得用删除功能、跳过检查或伪造结果规避失败",
                    "完成后重新运行平台检查并交给独立验收",
                ],
            )
        )
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
