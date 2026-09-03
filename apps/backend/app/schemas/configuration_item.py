from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from app.models.configuration_item import ConfigurationItemState, ConfigurationItemType


class ConfigurationItemRegistration(BaseModel):
    """岗位向 ConfigurationManager 提交的一份正式成果。"""

    model_config = ConfigDict(extra="forbid")

    semantic_type: ConfigurationItemType
    schema_version: int = Field(default=1, ge=1)
    payload: dict[str, JsonValue] = Field(min_length=1)
    upstream_item_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("upstream_item_ids")
    @classmethod
    def validate_upstream_item_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 40 for value in normalized):
            raise ValueError("上游 ConfigurationItem ID 格式不正确")
        if len(normalized) != len(set(normalized)):
            raise ValueError("上游 ConfigurationItem ID 不能重复")
        return normalized


class ConfigurationItemOut(BaseModel):
    """已登记成果的对外视图。"""

    model_config = ConfigDict(from_attributes=True)

    item_id: str
    project_id: int
    producer_run_id: str
    semantic_type: ConfigurationItemType
    version: int
    schema_version: int
    payload: dict[str, JsonValue]
    content_hash: str
    upstream_item_ids: list[str]
    state: ConfigurationItemState
    unusable_reason: str | None
    unusable_at: datetime | None
    created_at: datetime
