"""Engineering dependency plans, distinct from product requirement order."""

from __future__ import annotations

import hashlib
import json
import re
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
    requirement_ids: list[RequirementId] = Field(
        max_length=50,
        description="关联获批需求以追踪来源；修复项可由冻结修复请求追踪。",
    )
    provides_requirement_ids: list[RequirementId] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "明确提供的前置能力，必须是 requirement_ids 的子集并有 acceptance/deliverables；"
            "只用于前置依赖，不替代 feature 功能覆盖。"
            "视觉基础的需求关联默认不提供业务能力。声明后完成该项必须通过集成检查。"
        ),
    )
    title: PlanText
    kind: Literal["foundation", "visual_foundation", "feature", "repair"] = "feature"
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


def planning_item(spec: AppSpec, *, repair_request: str | None = None) -> WorkItem:
    """Decision context only; never persisted as an executable source task."""
    if repair_request is not None:
        return WorkItem(
            id="engineering_plan",
            requirement_ids=[],
            title="根据冻结缺陷请求和现有源码提交限域修复计划",
            kind="repair",
            acceptance=repair_targets(repair_request),
        )
    return WorkItem(
        id="engineering_plan",
        requirement_ids=[feature.id for feature in spec.features],
        title="根据冻结需求、设计与现有源码提交工程依赖计划",
    )


def repair_targets(content: str) -> list[str]:
    """Keep each numbered user defect intact; otherwise use one atomic request."""
    lines = content.strip().splitlines()
    numbered = re.compile(r"^\s*(?:[-*]\s*)?\d{1,2}[.、)]\s*(.+)$")
    targets: list[str] = []
    current: list[str] = []
    for line in lines:
        match = numbered.match(line)
        if match:
            if current:
                targets.append(" ".join(" ".join(current).split()))
            current = [match.group(1)]
        elif current and line.strip():
            current.append(line.strip())
    if current:
        targets.append(" ".join(" ".join(current).split()))
    # A long enumerated document is safer as one target than a truncated list.
    return targets if 1 <= len(targets) <= 20 else [" ".join(content.split())]


def _hashed_plan(items: list[PlannedWorkItem]) -> dict[str, Any]:
    payload = [item.model_dump(mode="json") for item in items]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {"items": payload, "content_hash": digest}


def suggested_repair_plan(
    spec: AppSpec,
    content: str,
    *,
    completed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A bounded proposal from the frozen defect request, not the original feature list."""
    used = {str(item.get("id")) for item in completed or []}
    items: list[PlannedWorkItem] = []
    for index, target in enumerate(repair_targets(content), 1):
        item_id = f"repair_{index}"
        suffix = 1
        while item_id in used:
            suffix += 1
            item_id = f"repair_{index}_{suffix}"
        used.add(item_id)
        items.append(
            PlannedWorkItem(
                id=item_id,
                title=target,
                kind="repair",
                requirement_ids=[],
                acceptance=[target],
                deliverables=[
                    "只修改解决该缺陷所需的源码，并保留已获批且已通过的行为",
                    "用当前源码运行完整集成检查，再交独立验收",
                ],
            )
        )
    validate_delivery_plan(
        spec,
        [WorkItem.model_validate(row) for row in completed or []]
        + [WorkItem(**item.model_dump()) for item in items],
        repair_request=content,
    )
    return _hashed_plan(items)


def suggested_delivery_plan(
    spec: AppSpec,
    design: SystemDesign | None = None,
    *,
    repair_request: str | None = None,
    completed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A validated initial proposal derived solely from frozen requirements and edges.

    It is neither accepted work nor completion evidence. The engineer must explicitly
    adopt this exact hash or submit its own graph through the normal validation gate.
    """
    if repair_request is not None:
        return suggested_repair_plan(spec, repair_request, completed=completed)
    ids = {feature.id for feature in spec.features}
    visual_id = "visual_foundation"
    suffix = 0
    while visual_id in ids:
        suffix += 1
        visual_id = f"visual_foundation_{suffix}"
    dependencies = {feature.id: set[str]() for feature in spec.features}
    if design is not None:
        design.validate_requirement_dependencies(spec)
        for edge in design.implementation_dependencies:
            dependencies[edge.dependent_requirement_id].add(edge.prerequisite_requirement_id)
    available: dict[str, set[str]] = {}
    remaining = dict(dependencies)
    while remaining:
        for key in [key for key, deps in remaining.items() if deps <= available.keys()]:
            parents = remaining.pop(key)
            available[key] = {key} | {req for parent in parents for req in available[parent]}
    items = [
        PlannedWorkItem(
            id=visual_id,
            title="建立或验证复用共享视觉基础",
            kind="visual_foundation",
            requirement_ids=[feature.id for feature in spec.features],
            acceptance=["共享样式与获批界面要求一致，已有基础优先验证复用"],
            deliverables=["当前工作区的主题、字体、间距及共享样式"],
        ),
        *[
            PlannedWorkItem(
                id=feature.id,
                title=feature.text,
                requirement_ids=[feature.id],
                depends_on=[visual_id, *sorted(dependencies[feature.id])],
                rationale="按冻结架构中的前置依赖实施获批功能",
                acceptance=[
                    criterion.text
                    for criterion in spec.acceptance_criteria
                    if feature.id in criterion.source_ids
                    and set(criterion.source_ids) <= available[feature.id]
                ]
                or [feature.text],
                deliverables=["实现该获批功能所需的源码及集成检查；复用已验证的前置能力"],
            )
            for feature in spec.features
        ],
    ]
    # Use the same authority as model-authored graphs. No fallback may waive a rule.
    validate_delivery_plan(spec, [WorkItem(**item.model_dump()) for item in items], design)
    return _hashed_plan(items)


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


def migrate_implementation_repair_plan(checkpoint: dict[str, Any], request: dict[str, Any]) -> bool:
    """Retire a legacy full-feature repair graph without erasing completed work."""
    identity = [request.get("revision_id"), request.get("message_id")]
    if checkpoint.get("delivery_plan_scope") == "implementation_repair":
        if checkpoint.get("repair_plan_identity") != identity:
            raise ConflictException("修复计划与冻结的用户请求身份不一致")
        return False
    previous = checkpoint.get("work_items") or []
    completed = [row for row in previous if row.get("status") == "completed"]
    if previous:
        checkpoint.setdefault("delivery_plan_history", []).append(
            {
                "reason": "旧全功能计划已由冻结实现修复请求限域；已完成工作保留",
                "items": previous,
                "model_turn": checkpoint.get("model_turns", 0),
                "superseded": True,
            }
        )
    checkpoint["work_items"] = completed
    checkpoint["current_work_item_id"] = None
    checkpoint["needs_delivery_plan"] = True
    checkpoint["delivery_plan_scope"] = "implementation_repair"
    checkpoint["repair_plan_identity"] = identity
    # These are consecutive-stall counters for the retired node, not spent budgets.
    checkpoint["no_progress_steps"] = 0
    checkpoint["steps_without_delivery"] = 0
    return True


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
    spec: AppSpec,
    items: list[WorkItem],
    design: SystemDesign | None = None,
    *,
    repair_request: str | None = None,
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
        if not item.requirement_ids and item.kind != "repair":
            issues.append({"code": "MISSING_REQUIREMENT_LINK", "work_item_id": item.id})
        if repair_request is None and item.kind == "repair":
            issues.append({"code": "UNEXPECTED_REPAIR_ITEM", "work_item_id": item.id})
        unknown = set(item.requirement_ids) - feature_ids
        invalid = set(item.depends_on) - (set(ids) - {item.id})
        invalid_provisions = set(item.provides_requirement_ids) - set(item.requirement_ids)
        if invalid_provisions or (
            item.provides_requirement_ids and (not item.acceptance or not item.deliverables)
        ):
            issues.append(
                {
                    "code": "INVALID_REQUIREMENT_PROVISION",
                    "work_item_id": item.id,
                    "field": "provides_requirement_ids",
                    "unlinked_requirement_ids": sorted(invalid_provisions),
                    "expected": (
                        "只能声明关联需求中的真实前置能力，并填写 acceptance 与 deliverables"
                    ),
                }
            )
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
        raise DeliveryPlanError("工程计划的需求关联、能力声明或依赖引用无效", issues)
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
    if repair_request is not None:
        repair_items = [item for item in items if item.kind == "repair"]
        if not repair_items or len(repair_items) > 20:
            issues.append(
                {
                    "code": "REPAIR_ITEM_COUNT",
                    "actual": len(repair_items),
                    "expected": "将本次冻结修复请求拆为 1 至 20 个修复工作项",
                }
            )
        unrelated = [
            item.id for item in items if item.status != "completed" and item.kind != "repair"
        ]
        if unrelated:
            issues.append(
                {
                    "code": "OUT_OF_SCOPE_REPAIR_ITEMS",
                    "work_item_ids": unrelated,
                    "expected": "本次只提交 kind=repair 的待做项；旧已完成项由平台保留",
                }
            )
        observations = [" ".join(" ".join(item.acceptance).split()) for item in repair_items]
        for target in repair_targets(repair_request):
            if not any(target in observation for observation in observations):
                issues.append(
                    {
                        "code": "MISSING_REPAIR_TARGET",
                        "target": target,
                        "expected": "在修复项 acceptance 中保留冻结缺陷的完整原文",
                    }
                )
        if issues:
            raise DeliveryPlanError("实现修复计划未覆盖冻结请求或包含未授权工作项", issues)
        return
    covered = {req for item in items if item.kind == "feature" for req in item.requirement_ids}
    if covered != feature_ids:
        missing = feature_ids - covered
        issues.append(
            {
                "code": "MISSING_FEATURE_COVERAGE",
                "missing_requirement_ids": sorted(missing),
                "linked_non_feature_items": [
                    {
                        "work_item_id": item.id,
                        "kind": item.kind,
                        "requirement_ids": sorted(set(item.requirement_ids) & missing),
                        "provides_requirement_ids": sorted(
                            set(item.provides_requirement_ids) & missing
                        ),
                    }
                    for item in items
                    if item.kind != "feature" and set(item.requirement_ids) & missing
                ],
                "expected": (
                    "全部获批功能必须由 kind=feature 的 requirement_ids 覆盖。"
                    "foundation/visual_foundation 的关联仅用于追踪；"
                    "provides_requirement_ids 只满足前置依赖，不计入功能覆盖。"
                ),
                "suggested_action": (
                    "保留仍需复用的基础任务及其能力声明，新增或扩展真正实现这些需求的 feature，"
                    "写明用户可见的实现与验收，并依赖相应基础任务。"
                    "若基础已经提供所需能力，feature 负责页面接入或用户流程验证，无需重建基础。"
                    "不要只追加需求标签或来回修改同一项的 kind；提交前同时检查功能覆盖、"
                    "视觉基础及冻结的前置依赖。"
                ),
            }
        )
    visual = {item.id for item in items if item.kind == "visual_foundation"}
    if not visual:
        issues.append(
            {
                "code": "MISSING_VISUAL_FOUNDATION",
                "feature_work_item_ids": [item.id for item in items if item.kind == "feature"],
                "expected": "先建立或验证复用视觉基础；未完成 feature 必须以它为祖先任务。",
                "suggested_action": (
                    "同时保留 feature 对获批功能的覆盖，并新增或恢复独立 visual_foundation。"
                    "若刚把视觉基础改成 feature，分别表达共享基础与用户可见实现/验收，"
                    "让 feature 依赖基础；仅改回 kind 会再次丢失功能覆盖。"
                    "基础确实提供架构前置能力时保留 provides_requirement_ids，"
                    "该声明仍不替代 feature 覆盖。"
                ),
            }
        )
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
                    dependency.prerequisite_requirement_id
                    in (
                        by_id[parent].provides_requirement_ids
                        or (
                            by_id[parent].requirement_ids
                            if by_id[parent].kind != "visual_foundation"
                            else []
                        )
                    )
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
                            "linked_ancestor_ids": sorted(
                                parent
                                for parent in ancestors[item.id]
                                if dependency.prerequisite_requirement_id
                                in by_id[parent].requirement_ids
                            ),
                            "expected": (
                                "前置能力必须由祖先工作项提供；视觉基础仅关联需求不算提供能力。"
                                "同项包含不计作前置。"
                            ),
                            "suggested_action": (
                                "若视觉基础确实实现此前置，"
                                "在其 provides_requirement_ids 声明该需求，"
                                "并给出可检查的 acceptance/deliverables；"
                                "否则依赖真正实现此前置的任务。"
                                "同项包含前后置时拆分任务。不要仅添加关联标签。"
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
    *,
    repair_request: str | None = None,
) -> list[dict[str, Any]]:
    """Models own pending plans, never completion state or approved coverage."""
    items = [WorkItem(**item.model_dump()) for item in proposed]
    by_id = {item.id: item for item in items}
    for raw in previous:
        old = WorkItem.model_validate(raw)
        replacement = by_id.get(old.id)
        if old.status == "completed":
            if replacement is None and repair_request is not None:
                items.insert(0, old)
                by_id[old.id] = old
                continue
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
    validate_delivery_plan(spec, items, design, repair_request=repair_request)
    return work_items_to_json(items)


def work_items_to_json(items: list[WorkItem]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items]
