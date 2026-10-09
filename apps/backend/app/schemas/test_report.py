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


class TestVerification(BaseModel):
    """Execution evidence joining a frozen test to acceptance and runner output."""

    model_config = ConfigDict(extra="forbid")

    test_id: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    acceptance_ids: list[str] = Field(min_length=1, max_length=50)
    check_id: str = Field(min_length=1, max_length=64)
    status: VerificationStatus
    command: ShortReportText
    evidence: ReportText
    artifact_paths: list[str] = Field(default_factory=list, max_length=20)


class DefectRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defect_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    severity: DefectSeverity
    title: ShortReportText
    description: ReportText
    evidence: ReportText
    related_requirement_ids: list[str] = Field(default_factory=list, max_length=50)


class TestChallenge(BaseModel):
    """Explicit escalation when a frozen test conflicts with approved intent."""

    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    test_ids: list[str] = Field(min_length=1, max_length=50)
    reason: ReportText
    requested_resolution: ReportText


class TestReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code_item_id: str = Field(min_length=1, max_length=40)
    source_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    test_plan_item_id: str | None = Field(default=None, min_length=1, max_length=40)
    test_hash: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    requirement_results: list[RequirementVerification] = Field(min_length=1, max_length=100)
    check_results: list[CheckVerification] = Field(min_length=1, max_length=30)
    test_results: list[TestVerification] = Field(default_factory=list, max_length=200)
    regression_test_ids: list[str] = Field(default_factory=list, max_length=200)
    defects: list[DefectRecord] = Field(default_factory=list, max_length=100)
    test_challenges: list[TestChallenge] = Field(default_factory=list, max_length=50)
    quality_conclusion: QualityConclusion
    summary: ReportText
    residual_risks: list[ReportText] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_conclusion_and_identity(self) -> Self:
        for values, label in (
            ([item.requirement_id for item in self.requirement_results], "需求验证"),
            ([item.check_id for item in self.check_results], "检查"),
            ([item.test_id for item in self.test_results], "测试"),
            ([item.defect_id for item in self.defects], "缺陷"),
            ([item.challenge_id for item in self.test_challenges], "测试质疑"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label}标识不能重复")
        statuses = (
            [item.status for item in self.requirement_results]
            + [item.status for item in self.check_results]
            + [item.status for item in self.test_results]
        )
        if (self.test_plan_item_id is None) != (self.test_hash is None):
            raise ValueError("test_plan_item_id 与 test_hash 必须同时提供")
        if len(self.regression_test_ids) != len(set(self.regression_test_ids)):
            raise ValueError("回归测试标识不能重复")
        if not set(self.regression_test_ids).issubset({item.test_id for item in self.test_results}):
            raise ValueError("回归范围引用了不存在的 test_id")
        known_test_ids = {item.test_id for item in self.test_results}
        if any(not set(item.test_ids).issubset(known_test_ids) for item in self.test_challenges):
            raise ValueError("测试质疑引用了不存在的 test_id")
        if self.test_challenges and self.quality_conclusion != QualityConclusion.BLOCKED:
            raise ValueError("存在测试质疑时质量结论必须为 blocked")
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
