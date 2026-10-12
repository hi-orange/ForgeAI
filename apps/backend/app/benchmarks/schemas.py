from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.project_message_classification import ProjectMessageCategory
from app.schemas.leader import NextActionKind


class BenchmarkExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: ProjectMessageCategory
    action: NextActionKind
    architect_required: bool
    end_to_end_success: bool
    security_should_block: bool = False
    max_repair_rounds: int = Field(default=3, ge=0, le=20)


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    prompt: str = Field(min_length=1, max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    expectation: BenchmarkExpectation


class BenchmarkSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite_id: str
    version: str
    description: str
    cases: list[BenchmarkCase] = Field(min_length=1)


class BenchmarkObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    suite_version: str
    category: ProjectMessageCategory | None = None
    action: NextActionKind | None = None
    architect_used: bool | None = None
    end_to_end_success: bool | None = None
    security_blocked: bool | None = None
    repair_rounds: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_microusd: int | None = Field(default=None, ge=0)
    trajectory_id: str | None = None


class BenchmarkMetric(BaseModel):
    name: str
    numerator: int
    denominator: int
    value: float | None


class BenchmarkCaseResult(BaseModel):
    case_id: str
    status: Literal["passed", "failed", "missing"]
    failed_checks: list[str]


class BenchmarkReport(BaseModel):
    suite_id: str
    suite_version: str
    suite_hash: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metrics: list[BenchmarkMetric]
    cases: list[BenchmarkCaseResult]
    average_repair_rounds: float | None = None
    average_duration_ms: float | None = None
    total_tokens: int | None = None
    estimated_cost_microusd: int | None = None
