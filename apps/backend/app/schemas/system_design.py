"""Validated body of a system_design artifact."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

DesignText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]
ShortDesignText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class ModuleDesign(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: ShortDesignText
    responsibility: DesignText
    dependencies: list[ShortDesignText] = Field(default_factory=list, max_length=30)


class InterfaceDesign(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: ShortDesignText
    kind: Literal["http", "internal", "event"]
    purpose: DesignText
    method: str | None = Field(default=None, max_length=20)
    path: str | None = Field(default=None, max_length=500)
    inputs: list[ShortDesignText] = Field(default_factory=list, max_length=50)
    outputs: list[ShortDesignText] = Field(default_factory=list, max_length=50)


class DataStructureDesign(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: ShortDesignText
    purpose: DesignText
    fields: list[ShortDesignText] = Field(min_length=1, max_length=100)
    relationships: list[ShortDesignText] = Field(default_factory=list, max_length=50)


class TechnologyChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    area: ShortDesignText
    choice: ShortDesignText
    rationale: DesignText


class SystemDesign(BaseModel):
    """A concise but implementation-ready design derived from one approved app_spec."""

    model_config = ConfigDict(extra="forbid", strict=True)

    architecture_overview: DesignText
    architecture_notes: list[DesignText] = Field(min_length=1, max_length=50)
    modules: list[ModuleDesign] = Field(min_length=1, max_length=50)
    interfaces: list[InterfaceDesign] = Field(min_length=1, max_length=100)
    data_structures: list[DataStructureDesign] = Field(min_length=1, max_length=100)
    technology_choices: list[TechnologyChoice] = Field(min_length=1, max_length=30)
    constraints: list[DesignText] = Field(default_factory=list, max_length=50)
