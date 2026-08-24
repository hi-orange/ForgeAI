import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EditableStyleName = Literal[
    "color",
    "background-color",
    "border-color",
    "font-size",
    "font-weight",
    "font-family",
    "text-align",
    "margin-top",
    "margin-right",
    "margin-bottom",
    "margin-left",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
    "gap",
    "border-radius",
]


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


class WebsiteElementChanges(BaseModel):
    text: str | None = Field(default=None, max_length=2000)
    styles: dict[EditableStyleName, str] = Field(
        default_factory=dict,
        max_length=17,
    )

    @field_validator("styles")
    @classmethod
    def validate_color_values(
        cls,
        value: dict[EditableStyleName, str],
    ) -> dict[EditableStyleName, str]:
        color_properties = {"color", "background-color", "border-color"}
        length_properties = {
            "font-size",
            "margin-top",
            "margin-right",
            "margin-bottom",
            "margin-left",
            "padding-top",
            "padding-right",
            "padding-bottom",
            "padding-left",
            "gap",
            "border-radius",
        }
        allowed_weights = {"100", "200", "300", "400", "500", "600", "700", "800", "900"}
        allowed_alignments = {"left", "center", "right", "justify"}
        allowed_fonts = {"Arial", "Georgia", "Inter", "system-ui", "sans-serif", "serif"}
        length_pattern = r"^-?\d+(?:\.\d+)?(?:px|rem|em|%)$"
        for name, style_value in value.items():
            if name in color_properties:
                if len(style_value) != 7 or not style_value.startswith("#"):
                    raise ValueError("颜色必须使用 #RRGGBB 格式")
                try:
                    int(style_value[1:], 16)
                except ValueError as exc:
                    raise ValueError("颜色必须使用 #RRGGBB 格式") from exc
            elif name in length_properties:
                if not re.fullmatch(length_pattern, style_value):
                    raise ValueError("尺寸必须包含合法的 CSS 单位")
            elif name == "font-weight" and style_value not in allowed_weights:
                raise ValueError("不支持的字重")
            elif name == "text-align" and style_value not in allowed_alignments:
                raise ValueError("不支持的文本对齐方式")
            elif name == "font-family" and style_value not in allowed_fonts:
                raise ValueError("不支持的字体")
        return value


class WebsiteElementPatch(BaseModel):
    element_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")
    changes: WebsiteElementChanges


class ProjectWebsiteEdit(BaseModel):
    base_revision: int = Field(ge=0)
    patches: list[WebsiteElementPatch] = Field(min_length=1, max_length=50)


class ProjectElementAiEdit(BaseModel):
    element_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")
    tag_name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9-]{0,30}$")
    text: str = Field(default="", max_length=2000)
    text_editable: bool = True
    styles: dict[EditableStyleName, str] = Field(default_factory=dict, max_length=17)
    instruction: str = Field(min_length=1, max_length=2000)


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
    validation_report: str | None = None
    website_revision: int = 0
    built_at: datetime | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class ProjectStartOut(BaseModel):
    project: ProjectOut
    workflow_id: str
    message: str = "Product Manager 已生成网站规格"
