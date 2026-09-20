from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.app_spec import AppSpec, RequirementId, RequirementText
from app.schemas.product_manager_workflow import ProductManagerWorkflowResult, WorkflowId


class RequirementsExecute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: int = Field(gt=0)
    recovery_execution_id: WorkflowId | None = None


class RequirementsSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=8000)
    client_message_id: str = Field(min_length=1, max_length=100)

    @field_validator("content", "client_message_id")
    @classmethod
    def strip_non_empty_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能为空")
        return stripped


class RequirementsStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: int | None = Field(default=None, gt=0)


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


class EngineeringActivity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    label: str
    detail: str = ""
    ok: bool = True


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
        "design_running",
        "engineering_running",
        "engineering_generated",
    ] = "not_started"
    execution_id: str | None = None
    execution_expires_at: datetime | None = None
    error: str | None = None
    result: ProductManagerWorkflowResult | None = None
    app_spec: AppSpec | None = None
    # The template is visible as soon as the isolated workspace exists; code_ready means a
    # successful business-code write has happened in this run.
    workspace_ready: bool = False
    code_ready: bool = False
    workspace_path: str | None = None
    activities: list[EngineeringActivity] = Field(default_factory=list)
