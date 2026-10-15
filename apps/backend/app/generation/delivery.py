"""Engineering dependency plans, distinct from product requirement order."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.exceptions import ConflictException
from app.schemas.app_spec import AppSpec, RequirementId
from app.schemas.engineering_diagnostic import EngineeringDiagnostic
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


class DeliveryPlanError(ConflictException):
    """All currently detectable violations, with an actionable location."""

    def __init__(self, message: str, issues: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.diagnostic = EngineeringDiagnostic(
            category="plan",
            disposition="repair",
            code="INVALID_DELIVERY_PLAN",
            summary=message,
            issues=issues,
            verification=(
                "修正完整 items 后重新调用 update_delivery_plan；保留全部获批需求与已完成项。"
            ),
        )


def validate_delivery_plan(
    spec: AppSpec, items: list[WorkItem], design: SystemDesign | None = None
) -> None:
    feature_ids = {feature.id for feature in spec.features}
    if not feature_ids:
        raise DeliveryPlanError("获批需求没有可实现的功能", [{"code": "NO_APPROVED_FEATURES"}])
    if not items or len(items) > 100:
        raise DeliveryPlanError(
            "工程计划必须包含 1 至 100 项", [{"code": "ITEM_COUNT", "actual": len(items)}]
        )
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise DeliveryPlanError(
            "工作单元 id 重复",
            [
                {"code": "DUPLICATE_ID", "work_item_id": key}
                for key in sorted(set(ids))
                if ids.count(key) > 1
            ],
        )
    issues: list[dict[str, Any]] = []
    for item in items:
        unknown = set(item.requirement_ids) - feature_ids
        invalid = set(item.depends_on) - (set(ids) - {item.id})
        if unknown:
            issues.append(
                {
                    "code": "UNAPPROVED_REQUIREMENT",
                    "work_item_id": item.id,
                    "field": "requirement_ids",
                    "actual": sorted(unknown),
                    "expected": "只能引用获批功能",
                }
            )
        if invalid:
            issues.append(
                {
                    "code": "INVALID_DEPENDENCY_TARGET",
                    "work_item_id": item.id,
                    "field": "depends_on",
                    "actual": sorted(invalid),
                    "expected": "引用存在且不是自身的工作项",
                }
            )
    if issues:
        raise DeliveryPlanError("工程计划引用了未获批功能、自身或不存在的任务", issues)
    ancestors: dict[str, set[str]] = {}
    remaining = {item.id: set(item.depends_on) for item in items}
    while remaining:
        ready = [key for key, deps in remaining.items() if deps <= ancestors.keys()]
        if not ready:
            raise DeliveryPlanError(
                "工程计划存在循环依赖",
                [
                    {
                        "code": "CYCLIC_DEPENDENCY",
                        "work_item_id": key,
                        "unresolved_dependencies": sorted(deps - ancestors.keys()),
                        "expected": "检查这些未就绪节点的依赖方向并消除环；节点也可能仅被环阻塞",
                    }
                    for key, deps in remaining.items()
                ],
            )
        for key in ready:
            deps = remaining.pop(key)
            ancestors[key] = deps | {parent for dep in deps for parent in ancestors[dep]}
    covered = {req for item in items if item.kind == "feature" for req in item.requirement_ids}
    if covered != feature_ids:
        issues.append(
            {
                "code": "MISSING_FEATURE_COVERAGE",
                "missing_requirement_ids": sorted(feature_ids - covered),
                "expected": "工程计划必须以功能任务覆盖全部获批功能，不能只交付基础设施",
            }
        )
    visual = {item.id for item in items if item.kind == "visual_foundation"}
    if not visual:
        issues.append({"code": "MISSING_VISUAL_FOUNDATION", "expected": "先建立或验证复用视觉基础"})
    for item in items:
        if (
            item.kind == "feature"
            and item.status != "completed"
            and not (visual & ancestors[item.id])
        ):
            issues.append(
                {
                    "code": "MISSING_VISUAL_DEPENDENCY",
                    "work_item_id": item.id,
                    "depends_on": item.depends_on,
                    "expected": "功能任务必须依赖视觉基础",
                }
            )
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
                    issues.append(
                        {
                            "code": "MISSING_REQUIREMENT_DEPENDENCY",
                            "work_item_id": item.id,
                            "prerequisite_requirement_id": dependency.prerequisite_requirement_id,
                            "dependent_requirement_id": dependency.dependent_requirement_id,
                            "depends_on": item.depends_on,
                            "prerequisite_in_same_item": dependency.prerequisite_requirement_id
                            in item.requirement_ids,
                            "reason": dependency.reason,
                            "expected": (
                                "前置需求必须由非视觉基础的祖先工作项实现；同项包含不计作前置。"
                            ),
                            "suggested_action": (
                                "若前置与后续在同项，拆分实现并建立 depends_on；"
                                "否则补齐实际前置任务依赖。不要只添加需求标签。"
                            ),
                        }
                    )
    if issues:
        raise DeliveryPlanError(
            "工程计划校验失败：检查全部 conflicts 中的覆盖、视觉基础及前置依赖", issues
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
                raise DeliveryPlanError(
                    "调整计划不能删除或改写已完成任务",
                    [
                        {
                            "code": "COMPLETED_ITEM_CHANGED",
                            "work_item_id": old.id,
                            "expected": PlannedWorkItem.model_validate(
                                old.model_dump(exclude={"status", "summary"})
                            ).model_dump(mode="json"),
                        }
                    ],
                )
            replacement.status = old.status
            replacement.summary = old.summary
    validate_delivery_plan(spec, items, design)
    return work_items_to_json(items)


def work_items_to_json(items: list[WorkItem]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items]
