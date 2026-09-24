"""Identity and provenance of one generated source result."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

CODE_ARTIFACT_SCHEMA_VERSION = 1


class SourceFileIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1000)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class CodeArtifact(BaseModel):
    """A content-addressed source manifest without embedding source bodies in SQL."""

    model_config = ConfigDict(extra="forbid")

    source_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    workspace_key: str = Field(min_length=1, max_length=200)
    template_version: str = Field(min_length=1, max_length=100)
    tool_strategy_version: str = Field(min_length=1, max_length=100)
    files: list[SourceFileIdentity] = Field(min_length=1, max_length=400)
    modified_files: list[str] = Field(default_factory=list, max_length=80)
    base_revision_id: str | None = Field(default=None, max_length=100)
