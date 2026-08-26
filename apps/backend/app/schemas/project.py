from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    """Each home submission creates a distinct project row."""

    prompt: str = Field(min_length=1, max_length=8000)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    description: str | None = None
    prompt: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime
