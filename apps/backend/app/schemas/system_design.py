"""Validated body of a system_design artifact."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.app_spec import AppSpec, RequirementId

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


class ImplementationDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    prerequisite_requirement_id: RequirementId
    dependent_requirement_id: RequirementId
    reason: DesignText


class OptionalCapability(BaseModel):
    """An explicit implementation choice linked to approved product behavior."""

    model_config = ConfigDict(extra="forbid", strict=True)

    capability_id: Literal["auth-session-v1"]
    requirement_ids: list[RequirementId] = Field(min_length=1, max_length=50)
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
    implementation_dependencies: list[ImplementationDependency] = Field(
        default_factory=list, max_length=100
    )
    optional_capabilities: list[OptionalCapability] = Field(default_factory=list, max_length=10)

    def validate_requirement_dependencies(self, spec: AppSpec) -> None:
        ids = {item.id for item in spec.features}
        selected: set[str] = set()
        for capability in self.optional_capabilities:
            if capability.capability_id in selected:
                raise ValueError("可选能力不能重复选择")
            selected.add(capability.capability_id)
            linked = capability.requirement_ids
            if len(linked) != len(set(linked)) or not set(linked) <= ids:
                raise ValueError("可选能力必须关联不同且存在的获批功能 id")
        edges: dict[str, set[str]] = {key: set() for key in ids}
        for dependency in self.implementation_dependencies:
            before = dependency.prerequisite_requirement_id
            after = dependency.dependent_requirement_id
            if before not in ids or after not in ids or before == after:
                raise ValueError("设计依赖必须关联不同的获批功能 id")
            edges[after].add(before)
        while edges:
            ready = {key for key, deps in edges.items() if not deps}
            if not ready:
                raise ValueError("设计包含循环工程依赖")
            edges = {key: deps - ready for key, deps in edges.items() if key not in ready}
