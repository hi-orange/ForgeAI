from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SecuritySeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SecurityConclusion(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class SecurityFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str = Field(min_length=1, max_length=100)
    severity: SecuritySeverity
    path: str = Field(min_length=1, max_length=1000)
    line: int | None = Field(default=None, ge=1)
    message: str = Field(min_length=1, max_length=500)
    evidence: str | None = Field(default=None, max_length=300)


class SecurityScanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scanner_version: str = Field(min_length=1, max_length=64)
    conclusion: SecurityConclusion
    findings: list[SecurityFinding] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_conclusion(self) -> "SecurityScanPayload":
        blocking = any(
            f.severity in {SecuritySeverity.HIGH, SecuritySeverity.CRITICAL} for f in self.findings
        )
        if (self.conclusion == SecurityConclusion.FAILED) != blocking:
            raise ValueError("security conclusion must match high/critical findings")
        return self


class SecurityReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    report_id: str
    project_id: int
    build_run_id: str
    code_item_id: str
    source_hash: str
    scanner_version: str
    conclusion: SecurityConclusion
    findings: list[SecurityFinding]
    error: str | None = None
    created_at: datetime
