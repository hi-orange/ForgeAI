"""Identity and provenance of one generated source result."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

CODE_ARTIFACT_SCHEMA_VERSION = 2


class SourceFileIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1000)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class CodeArtifact(BaseModel):
    """Identity and durable-object pointer for one exact generated source tree."""

    model_config = ConfigDict(extra="forbid")

    source_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    # Optional only for schema-v1 rows. Newly published v2 artifacts always set
    # all three fields and can be rehydrated after disposable workspace loss.
    source_artifact_id: str | None = Field(default=None, min_length=1, max_length=40)
    artifact_format: str | None = Field(default=None, min_length=1, max_length=32)
    artifact_size_bytes: int | None = Field(default=None, ge=0)
    workspace_key: str = Field(min_length=1, max_length=200)
    template_version: str = Field(min_length=1, max_length=100)
    tool_strategy_version: str = Field(min_length=1, max_length=100)
    files: list[SourceFileIdentity] = Field(min_length=1, max_length=400)
    modified_files: list[str] = Field(default_factory=list, max_length=80)
    base_revision_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_durable_pointer(self) -> CodeArtifact:
        values = (
            self.source_artifact_id,
            self.artifact_format,
            self.artifact_size_bytes,
        )
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("durable source artifact fields must be provided together")
        return self
