from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.project_message import ProjectMessageSender


class ProjectMessageCreate(BaseModel):
    """用户向一个已有项目追加的对话消息。"""

    model_config = ConfigDict(extra="forbid")

    client_message_id: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=8000)

    @field_validator("client_message_id", "content")
    @classmethod
    def strip_non_empty_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能为空")
        return stripped


class ProjectMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    sequence: int
    sender: ProjectMessageSender
    content: str
    client_message_id: str
    created_at: datetime
