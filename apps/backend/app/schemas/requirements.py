from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.app_spec import AppSpec
from app.schemas.product_manager_workflow import ProductManagerWorkflowResult, WorkflowId


class RequirementsExecute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: int = Field(gt=0)
    recovery_execution_id: WorkflowId | None = None


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
        "ready_for_design",
        "design_pending",
    ] = "not_started"
    execution_id: str | None = None
    execution_expires_at: datetime | None = None
    error: str | None = None
    result: ProductManagerWorkflowResult | None = None
    app_spec: AppSpec | None = None
