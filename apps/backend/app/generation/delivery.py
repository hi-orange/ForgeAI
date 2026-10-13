"""Engineering dependency plans, distinct from product requirement order."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.exceptions import ConflictException
from app.schemas.app_spec import AppSpec, RequirementId
from app.schemas.system_design import SystemDesign
from app.schemas.test_report import TestReport, VerificationStatus

PlanText = Annotated[str, StringConstraints(min_length=1, max_length=16000)]


class PlannedWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: RequirementId
    requirement_ids: list[RequirementId] = Field(min_length=1, max_length=50)
    title: PlanText
    kind: Literal["foundation", "visual_foundation", "feature"] = "feature"
    depends_on: list[RequirementId] = Field(default_factory=list, max_length=100)
    rationale: str = Field(default="", max_length=2000)
    acceptance: list[PlanText] = Field(default_factory=list, max_length=50)
    deliverables: list[PlanText] = Field(default_factory=list, max_length=50)


class WorkItem(PlannedWorkItem):
    # Persisted legacy and QA items are not model-submitted plan arguments.
    # Preserve their original identifiers and complete diagnostic evidence.
    id: str
    title: str
    acceptance: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    status: str = "pending"
    summary: str | None = None


def planning_item(spec: AppSpec) -> WorkItem:
    """Decision context only; never persisted as an executable source task."""
    return WorkItem(
        id="engineering_plan",
        requirement_ids=[feature.id for feature in spec.features],
        title="根据冻结需求、设计与现有源码提交工程依赖计划",
    )


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


def validate_delivery_plan(
    spec: AppSpec, items: list[WorkItem], design: SystemDesign | None = None
) -> None:
    feature_ids = {feature.id for feature in spec.features}
    if not feature_ids:
        raise ConflictException("获批需求没有可实现的功能")
    if not items or len(items) > 100:
        raise ConflictException("工程计划必须包含 1 至 100 项")
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise ConflictException("工作单元 id 重复")
    for item in items:
        if not set(item.requirement_ids) <= feature_ids:
            raise ConflictException("工程计划不能引用未获批功能")
        if item.id in item.depends_on or not set(item.depends_on) <= set(ids):
            raise ConflictException("工程依赖不能引用自身或不存在的任务")
    ancestors: dict[str, set[str]] = {}
    remaining = {item.id: set(item.depends_on) for item in items}
    while remaining:
        ready = [key for key, deps in remaining.items() if deps <= ancestors.keys()]
        if not ready:
            raise ConflictException("工程计划存在循环依赖")
        for key in ready:
            deps = remaining.pop(key)
            ancestors[key] = deps | {parent for dep in deps for parent in ancestors[dep]}
    covered = {
        req_id for item in items if item.kind == "feature" for req_id in item.requirement_ids
    }
    if covered != feature_ids:
        raise ConflictException("工程计划必须以功能任务覆盖全部获批功能，不能只交付基础设施")
    visual = {item.id for item in items if item.kind == "visual_foundation"}
    if not visual:
        raise ConflictException("先规划视觉基础的建立或验证复用，再实现业务界面")
    for item in items:
        if (
            item.kind == "feature"
            and item.status != "completed"
            and not (visual & ancestors[item.id])
        ):
            raise ConflictException("功能任务必须依赖视觉基础；已有主题也须先验证复用")
    if design is not None:
        design.validate_requirement_dependencies(spec)
        by_id = {item.id: item for item in items}
        for dependency in design.implementation_dependencies:
            for item in items:
                if (
                    item.kind != "feature"
                    or item.status == "completed"
                    or dependency.dependent_requirement_id not in item.requirement_ids
                ):
                    continue
                if not any(
                    by_id[parent].kind != "visual_foundation"
                    and dependency.prerequisite_requirement_id in by_id[parent].requirement_ids
                    for parent in ancestors[item.id]
                ):
                    raise ConflictException(
                        "工程计划缺少冻结设计要求的前置依赖：" + dependency.reason
                    )


def replace_delivery_plan(
    spec: AppSpec,
    proposed: list[PlannedWorkItem],
    previous: list[dict[str, Any]],
    design: SystemDesign | None = None,
) -> list[dict[str, Any]]:
    """Models own pending plans, never completion state or approved coverage."""
    items = [WorkItem(**item.model_dump()) for item in proposed]
    by_id = {item.id: item for item in items}
    for raw in previous:
        old = WorkItem.model_validate(raw)
        replacement = by_id.get(old.id)
        if old.status == "completed":
            if replacement is None or (
                PlannedWorkItem.model_validate(old.model_dump(exclude={"status", "summary"}))
                != PlannedWorkItem.model_validate(
                    replacement.model_dump(exclude={"status", "summary"})
                )
            ):
                raise ConflictException("调整计划不能删除或改写已完成任务")
            replacement.status = old.status
            replacement.summary = old.summary
    validate_delivery_plan(spec, items, design)
    return work_items_to_json(items)


def work_items_to_json(items: list[WorkItem]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items]
