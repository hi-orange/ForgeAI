from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    """主页每次提交需求都新建一条 project，不按名称/文案去重。"""

    prompt: str = Field(min_length=1, max_length=8000)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ProjectStart(BaseModel):
    """启动已有项目的 Agent Workflow（不创建新项目）。"""

    prompt: str | None = Field(default=None, min_length=1, max_length=8000)


class SectionSelection(BaseModel):
    page_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    section_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ProjectApproveSpec(BaseModel):
    selected_sections: list[SectionSelection] = Field(min_length=1, max_length=80)


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    description: str | None = None
    prompt: str | None = None
    prd: str | None = None
    approved_spec: str | None = None
    approved_at: datetime | None = None
    generated_files: str | None = None
    build_error: str | None = None
    built_at: datetime | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class ProjectStartOut(BaseModel):
    project: ProjectOut
    workflow_id: str
    message: str = "Product Manager 已生成网站规格"
