from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    """创建项目：提交用户需求（prompt）。"""

    prompt: str = Field(min_length=1, max_length=8000)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ProjectStart(BaseModel):
    """启动 Agent Workflow，可覆盖/补充需求。"""

    prompt: str | None = Field(default=None, min_length=1, max_length=8000)


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


class ProjectStartOut(BaseModel):
    project: ProjectOut
    workflow_id: str
    message: str = "已进入 Agent Workflow"
