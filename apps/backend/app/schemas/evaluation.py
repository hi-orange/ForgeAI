from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.project_message_classification import ProjectMessageCategory
from app.schemas.leader import NextActionKind


class EvalMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["available", "unavailable"]
    numerator: int | None = None
    denominator: int | None = None
    value: float | None = None
    unit: str = "rate"
    note: str | None = None


class AgentUsageSummary(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    duration_ms: int = 0
    estimated_cost_microusd: int | None = None
    model_turns: int = 0


class AgentEvaluationReport(BaseModel):
    project_id: int
    generated_at: datetime
    metrics: list[EvalMetric]
    usage: AgentUsageSummary


class AgentDecisionEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    decision_key: str
    project_id: int
    build_run_id: str | None = None
    message_id: int | None = None
    report_item_id: str | None = None
    category: str | None = None
    action: str
    reason_code: str
    decided_by: str
    recipient: str | None = None
    source_artifact_ids: list[str]
    required_inputs: list[str]
    risk_flags: list[str]
    decision_payload: dict[str, Any]
    status: str
    error: str | None = None
    created_at: datetime
    applied_at: datetime | None = None


class AgentEvalLabelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: int = Field(gt=0)
    expected_category: ProjectMessageCategory
    expected_action: NextActionKind | None = None
