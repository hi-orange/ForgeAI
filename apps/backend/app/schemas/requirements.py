from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.app_spec import AppSpec, RequirementId, RequirementText
from app.schemas.product_manager_workflow import ProductManagerWorkflowResult, WorkflowId


class RequirementsExecute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: int = Field(gt=0)
    recovery_execution_id: WorkflowId | None = None


class RequirementsApprovalSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: RequirementId
    text: RequirementText
    kind: Literal["feature", "data", "interface", "constraint"]
    acceptance: RequirementText | None = None


class RequirementsApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_message_id: WorkflowId
    goal: RequirementText
    selected: list[RequirementsApprovalSelection] = Field(min_length=1, max_length=50)


class RequirementsStatus(BaseModel):
    project_id: int
    run_id: str | None = None
    plan_id: str | None = None
    task_id: str | None = None
    message_id: int | None = None
    state: Literal[
        "not_started",
        "pending",
        "running",
        "retry_available",
        "stopped",
        "needs_user_input",
        "awaiting_approval",
        "ready_for_design",
        "design_pending",
    ] = "not_started"
    execution_id: str | None = None
    execution_expires_at: datetime | None = None
    error: str | None = None
    result: ProductManagerWorkflowResult | None = None
    app_spec: AppSpec | None = None
