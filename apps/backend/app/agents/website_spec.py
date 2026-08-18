from typing import Literal

from pydantic import BaseModel, Field


class ProductSummary(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=300)
    target_audience: str = Field(min_length=1, max_length=300)
    primary_goal: str = Field(min_length=1, max_length=300)


class WebsiteSection(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    type: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=500)
    content_points: list[str] = Field(default_factory=list, max_length=10)
    cta: str | None = Field(default=None, max_length=100)


class WebsitePage(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=100)
    path: str = Field(pattern=r"^/")
    purpose: str = Field(min_length=1, max_length=300)
    sections: list[WebsiteSection] = Field(min_length=1, max_length=10)


class SiteStructure(BaseModel):
    type: Literal["landing_page", "marketing_site", "content_site", "web_app"]
    language: str = Field(default="zh-CN", min_length=2, max_length=20)
    pages: list[WebsitePage] = Field(min_length=1, max_length=8)


class DesignDirection(BaseModel):
    style: str = Field(min_length=1, max_length=200)
    tone: str = Field(min_length=1, max_length=200)
    primary_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    accent_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    font_style: str = Field(min_length=1, max_length=200)


class WebsiteRequirements(BaseModel):
    features: list[str] = Field(default_factory=list, max_length=8)
    integrations: list[str] = Field(default_factory=list, max_length=8)
    excluded: list[str] = Field(default_factory=list, max_length=8)


class WebsiteSpecification(BaseModel):
    version: Literal["1.0"] = "1.0"
    product: ProductSummary
    site: SiteStructure
    design: DesignDirection
    requirements: WebsiteRequirements
    acceptance_criteria: list[str] = Field(min_length=1, max_length=8)
    assumptions: list[str] = Field(default_factory=list, max_length=5)
