import re
from hashlib import sha1
from typing import Annotated, Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

APP_SPEC_SCHEMA_VERSION: Final = 2

RequirementText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]
RequirementList = Annotated[list[RequirementText], Field(max_length=50)]
RequirementId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
    ),
]

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


class RequirementItem(BaseModel):
    """Stable checklist entry. Approval selects and edits by id, not by text equality."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: RequirementId = Field(description="稳定条目标识，批准时用于关联原条目")
    text: RequirementText = Field(description="用户可见的条目正文")


class AcceptanceCriterion(RequirementItem):
    """Acceptance clause linked to the feature ids it verifies."""

    source_ids: Annotated[list[RequirementId], Field(default_factory=list, max_length=50)] = Field(
        default_factory=list,
        description="对应的功能条目 id；不能只靠正文猜测来源",
    )


RequirementItemList = Annotated[list[RequirementItem], Field(max_length=50)]
AcceptanceList = Annotated[list[AcceptanceCriterion], Field(max_length=50)]


def _stable_id(prefix: str, text: str, index: int, used: set[str]) -> str:
    digest = sha1(f"{prefix}:{index}:{text}".encode()).hexdigest()[:10]
    candidate = f"{prefix}_{digest}"
    while candidate in used:
        digest = sha1(f"{candidate}:dup".encode()).hexdigest()[:10]
        candidate = f"{prefix}_{digest}"
    used.add(candidate)
    return candidate


def _sanitize_id(raw: object, prefix: str, text: str, index: int, used: set[str]) -> str:
    if isinstance(raw, str):
        cleaned = raw.strip().replace(" ", "_")
        if re.fullmatch(_ID_PATTERN, cleaned) and cleaned not in used:
            used.add(cleaned)
            return cleaned
    return _stable_id(prefix, text, index, used)


def _coerce_item(
    value: object, *, prefix: str, index: int, used: set[str], with_sources: bool
) -> dict[str, object]:
    if isinstance(value, AcceptanceCriterion):
        value = value.model_dump(mode="python")
    elif isinstance(value, RequirementItem):
        value = value.model_dump(mode="python")
    if isinstance(value, str):
        text = value.strip()
        item: dict[str, object] = {
            "id": _stable_id(prefix, text, index, used),
            "text": text,
        }
        if with_sources:
            item["source_ids"] = []
        return item
    if isinstance(value, dict):
        text_raw = value.get("text", value.get("label", value.get("title", "")))
        text = text_raw.strip() if isinstance(text_raw, str) else ""
        item = {
            "id": _sanitize_id(value.get("id"), prefix, text or prefix, index, used),
            "text": text,
        }
        if with_sources:
            sources = value.get("source_ids", [])
            item["source_ids"] = list(sources) if isinstance(sources, list) else []
        return item
    raise ValueError(f"{prefix} 条目格式不正确")


def _coerce_item_list(
    values: object, *, prefix: str, used: set[str], with_sources: bool = False
) -> list[dict[str, object]]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError(f"{prefix} 必须是列表")
    if len(values) > 50:
        raise ValueError(f"{prefix} 最多 50 项")
    return [
        _coerce_item(value, prefix=prefix, index=index, used=used, with_sources=with_sources)
        for index, value in enumerate(values)
    ]


class AppSpec(BaseModel):
    """产品意图的正文；约束文档结构，不规定应用必须有哪些页面或数据表。

    所有栏目都必须返回；用户未说明的内容可以为空列表，并通过 open_questions
    标明待确认事项。结构校验不等于已经证明模型没有遗漏或误解需求。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    goal: RequirementText = Field(description="应用要解决的问题和目标")
    target_users: RequirementList = Field(description="用户明确提到的使用人群")
    features: RequirementItemList = Field(description="用户可见的功能和行为，不是代码实现步骤")
    data_requirements: RequirementItemList = Field(description="需要记录、展示或处理的业务数据")
    interface_requirements: RequirementItemList = Field(description="用户提出的界面和交互要求")
    constraints: RequirementItemList = Field(description="权限、业务规则及其他明确约束")
    acceptance_criteria: AcceptanceList = Field(description="可观察、可检查且有需求依据的验收条件")
    open_questions: RequirementList = Field(description="信息缺失、冲突或歧义带来的待确认问题")

    @model_validator(mode="before")
    @classmethod
    def coerce_legacy_and_model_shapes(cls, data: Any) -> Any:
        """Accept model output that still uses plain strings or odd ids."""
        if not isinstance(data, dict):
            return data
        used: set[str] = set()
        payload = dict(data)
        for field, prefix, with_sources in (
            ("features", "feat", False),
            ("data_requirements", "data", False),
            ("interface_requirements", "ui", False),
            ("constraints", "con", False),
            ("acceptance_criteria", "ac", True),
        ):
            if field in payload:
                payload[field] = _coerce_item_list(
                    payload[field], prefix=prefix, used=used, with_sources=with_sources
                )
        return payload

    @model_validator(mode="after")
    def require_actionable_content_or_questions(self) -> Self:
        if not self.features and not self.open_questions:
            raise ValueError("尚无明确功能时，必须列出待确认问题")
        if not self.acceptance_criteria and not self.open_questions:
            raise ValueError("尚无验收条件时，必须列出待确认问题")
        ids = [
            item.id
            for group in (
                self.features,
                self.data_requirements,
                self.interface_requirements,
                self.constraints,
                self.acceptance_criteria,
            )
            for item in group
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("需求条目 id 必须在整份 app_spec 内唯一")
        feature_ids = {item.id for item in self.features}
        fixed_acceptance: list[AcceptanceCriterion] = []
        for criterion in self.acceptance_criteria:
            source_ids = [item_id for item_id in criterion.source_ids if item_id in feature_ids]
            if not source_ids and feature_ids:
                # Models often omit source_ids; keep the clause and attach all current features.
                source_ids = [item.id for item in self.features]
            fixed_acceptance.append(
                AcceptanceCriterion(id=criterion.id, text=criterion.text, source_ids=source_ids)
            )
        self.acceptance_criteria = fixed_acceptance
        return self
