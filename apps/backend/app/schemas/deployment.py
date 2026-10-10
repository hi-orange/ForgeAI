from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class DeploymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["local_docker"] = "local_docker"


class DeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    deployment_id: str
    project_id: int
    build_run_id: str
    revision: int
    provider: str
    status: Literal["queued", "building", "ready", "failed", "stopped", "rolled_back"]
    url: str | None = None
    domain: str | None = None
    image_reference: str | None = None
    secret_reference: str | None = None
    previous_deployment_id: str | None = None
    logs: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    deployed_at: datetime | None = None
