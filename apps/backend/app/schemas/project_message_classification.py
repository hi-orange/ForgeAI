from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.project_message_classification import ProjectMessageCategory


class ProjectMessageClassificationDecision(BaseModel):
    """ProjectManager 模型调用经过校验后的最小决策。"""

    model_config = ConfigDict(extra="forbid")

    category: ProjectMessageCategory
    decision_summary: str = Field(min_length=1, max_length=300)

    @field_validator("decision_summary")
    @classmethod
    def strip_decision_summary(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能为空")
        return stripped


class ProjectMessageClassificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    message_id: int
    category: ProjectMessageCategory
    decision_summary: str
    classifier_model: str
    prompt_version: str
    created_at: datetime
