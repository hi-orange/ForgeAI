"""Validated evidence and quality conclusion for one exact code result."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

ReportText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]
ShortReportText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    NOT_RUN = "not_run"


class DefectSeverity(StrEnum):
    BLOCKER = "blocker"
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"


class QualityConclusion(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class RequirementVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement_id: str = Field(min_length=1, max_length=64)
    status: VerificationStatus
    evidence: ReportText


class CheckVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=1, max_length=64)
    status: VerificationStatus
    evidence: ReportText


class DefectRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defect_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    severity: DefectSeverity
    title: ShortReportText
    description: ReportText
    evidence: ReportText
    related_requirement_ids: list[str] = Field(default_factory=list, max_length=50)


class TestReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code_item_id: str = Field(min_length=1, max_length=40)
    source_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    requirement_results: list[RequirementVerification] = Field(min_length=1, max_length=100)
    check_results: list[CheckVerification] = Field(min_length=1, max_length=30)
    defects: list[DefectRecord] = Field(default_factory=list, max_length=100)
    quality_conclusion: QualityConclusion
    summary: ReportText
    residual_risks: list[ReportText] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_conclusion_and_identity(self) -> Self:
        for values, label in (
            ([item.requirement_id for item in self.requirement_results], "需求验证"),
            ([item.check_id for item in self.check_results], "检查"),
            ([item.defect_id for item in self.defects], "缺陷"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label}标识不能重复")
        statuses = [item.status for item in self.requirement_results] + [
            item.status for item in self.check_results
        ]
        all_passed = all(status == VerificationStatus.PASSED for status in statuses)
        failed = any(status == VerificationStatus.FAILED for status in statuses)
        blocked = any(
            status in {VerificationStatus.BLOCKED, VerificationStatus.NOT_RUN}
            for status in statuses
        )
        if self.quality_conclusion == QualityConclusion.PASSED and (not all_passed or self.defects):
            raise ValueError("存在未通过项或缺陷时不能给出通过结论")
        if self.quality_conclusion == QualityConclusion.FAILED and not (failed or self.defects):
            raise ValueError("失败结论必须有失败证据或缺陷")
        if self.quality_conclusion == QualityConclusion.BLOCKED and not blocked:
            raise ValueError("阻断结论必须有未执行或阻断证据")
        return self
