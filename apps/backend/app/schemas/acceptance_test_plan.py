"""Immutable, implementation-independent acceptance test design."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

ACCEPTANCE_TEST_PLAN_SCHEMA_VERSION: Final = 2

TestText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
]


class TestLayer(StrEnum):
    BACKEND = "backend_pytest"
    API = "api_integration"
    FRONTEND = "frontend_vitest"
    E2E = "playwright_e2e"


class AcceptanceTestCase(BaseModel):
    """One stable test identity mapped to one or more approved acceptance clauses."""

    model_config = ConfigDict(extra="forbid")

    test_id: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    acceptance_ids: list[str] = Field(min_length=1, max_length=50)
    layer: TestLayer
    framework: str = Field(min_length=1, max_length=40)
    target_path: str = Field(min_length=1, max_length=500)
    command: str = Field(min_length=1, max_length=500)
    objective: TestText
    oracle: TestText


class AcceptanceTestPlan(BaseModel):
    """Frozen test ownership boundary created before Code Engineer starts work."""

    model_config = ConfigDict(extra="forbid")

    source_item_id: str = Field(min_length=1, max_length=40)
    tests: list[AcceptanceTestCase] = Field(min_length=1, max_length=200)
    # Frozen acceptance lives in this configuration item. Paths are optional and
    # only describe platform-owned assets when such assets actually exist.
    protected_paths: list[str] = Field(default_factory=list, max_length=20)
    regression_policy: TestText

    @model_validator(mode="after")
    def validate_identity_and_coverage(self) -> Self:
        test_ids = [item.test_id for item in self.tests]
        if len(test_ids) != len(set(test_ids)):
            raise ValueError("验收测试 test_id 不能重复")
        if len(self.protected_paths) != len(set(self.protected_paths)):
            raise ValueError("受保护测试路径不能重复")
        for item in self.tests:
            if len(item.acceptance_ids) != len(set(item.acceptance_ids)):
                raise ValueError(f"测试 {item.test_id} 的 acceptance_ids 不能重复")
        return self
