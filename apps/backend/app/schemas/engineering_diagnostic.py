"""Platform-authored feedback; never evidence of a model-declared repair."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EngineeringDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal[
        "plan", "build", "database", "api", "acceptance", "environment", "contract", "tool"
    ]
    disposition: Literal["repair", "restore_environment", "refresh_evidence", "needs_decision"]
    code: str
    summary: str
    issues: list[dict[str, Any]] = Field(default_factory=list)
    verification: str
    source_hash: str | None = None
    fingerprint: str = ""
