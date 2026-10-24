"""Versioned QA assets: approved oracles, scenarios and bounded executable scripts.

Scripts are data, not arbitrary Python/JS. Only the trusted isolated controller
can execute their HTTP/browser operations and produce assertion observations.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Identity = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,96}$")]
RuntimeName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
SQLIdentifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]

_PROTECTED_ENV_NAMES = frozenset(
    {"DATABASE_URL", "DEBUG", "PATH", "HOME", "USER", "SHELL", "PWD", "TMP", "TMPDIR"}
)
_PROTECTED_ENV_PREFIXES = (
    "PYTHON",
    "PIP_",
    "LD_",
    "NODE_",
    "NPM_",
    "FORGEAI_",
    "DOCKER_",
    "AWS_",
    "GOOGLE_",
    "OPENAI_",
    "UVICORN_",
    "ALEMBIC_",
)


class QAAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JSONAssertion(QAAsset):
    path: Text
    operator: Literal["equals", "contains", "not_contains", "length", "exists"]
    value: Any = None


class APIStep(QAAsset):
    kind: Literal["api"] = "api"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: Text
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    json_body: Any = None
    expect_status: int = Field(ge=100, le=599)
    assertions: list[JSONAssertion] = Field(default_factory=list, max_length=30)
    capture: dict[Identity, Text] = Field(default_factory=dict, max_length=20)


class BrowserStep(QAAsset):
    kind: Literal["browser"] = "browser"
    action: Literal[
        "navigate",
        "fill",
        "click",
        "select",
        "reload",
        "back",
        "expect_text",
        "expect_count",
        "expect_path",
        "expect_visible",
        "wait_for_visible",
    ]
    path: Text | None = None
    selector: Text | None = None
    value: str | None = Field(default=None, max_length=2000)
    count: int | None = Field(default=None, ge=0, le=10000)

    @model_validator(mode="after")
    def required_arguments(self) -> Self:
        if self.action in {"navigate", "expect_path"} and self.path is None:
            raise ValueError("浏览器路由缺失")
        if self.action in {
            "fill",
            "click",
            "select",
            "expect_count",
            "expect_visible",
            "wait_for_visible",
        }:
            if self.selector is None:
                raise ValueError("浏览器选择器缺失")
        if self.action == "wait_for_visible" and any(
            value is not None for value in (self.path, self.value, self.count)
        ):
            raise ValueError("纯同步步骤只接受 selector")
        if self.action in {"fill", "select", "expect_text"} and self.value is None:
            raise ValueError("浏览器输入或预期文本缺失")
        if self.action == "expect_text" and not (self.value or "").strip():
            raise ValueError("预期文本不能为空")
        if self.action == "expect_count" and self.count is None:
            raise ValueError("预期元素数量缺失")
        return self


class QALegacyRow(QAAsset):
    """Bounded declarative DML, inserted before upgrading an old schema."""

    table: SQLIdentifier
    values: dict[SQLIdentifier, Any] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def scalar_values_only(self) -> Self:
        for value in self.values.values():
            if value is not None and type(value) not in {str, int, float, bool}:
                raise ValueError("旧库夹具仅接受 JSON 标量")
            if isinstance(value, str) and len(value) > 2000:
                raise ValueError("旧库夹具字段过长")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("旧库夹具数值必须有限")
        return self


class QARuntime(QAAsset):
    """Scenario-specific startup and migration state under trusted runner control."""

    env: dict[RuntimeName, str] = Field(default_factory=dict, max_length=12)
    before_upgrade_revision: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_]{1,96}$")] | None
    ) = None
    seed_rows: list[QALegacyRow] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def safe_configuration(self) -> Self:
        for name, value in self.env.items():
            if name in _PROTECTED_ENV_NAMES or name.startswith(_PROTECTED_ENV_PREFIXES):
                raise ValueError(f"QA 不能覆盖执行器环境变量 {name}")
            if len(value) > 256 or re.search(r"[\x00-\x1f\x7f]", value):
                raise ValueError("QA 启动变量值无效")
        if self.before_upgrade_revision in {"head", "heads", "base"}:
            raise ValueError("旧库夹具必须指定具体 Alembic 修订")
        if self.seed_rows and self.before_upgrade_revision is None:
            raise ValueError("旧库夹具必须指定升级前修订")
        if any(row.table.lower() == "alembic_version" for row in self.seed_rows):
            raise ValueError("旧库夹具不能改写迁移版本表")
        if len(self.model_dump_json().encode("utf-8")) > 64 * 1024:
            raise ValueError("QA 场景运行夹具超过 64 KiB")
        return self


Step = Annotated[APIStep | BrowserStep, Field(discriminator="kind")]


class QAScenario(QAAsset):
    test_id: Identity
    acceptance_ids: list[Identity] = Field(min_length=1, max_length=50)
    objective: Text
    preconditions: list[Text] = Field(min_length=1, max_length=20)
    test_data: list[Text] = Field(min_length=1, max_length=20)
    steps: list[Text] = Field(min_length=1, max_length=50)
    expected_results: list[Text] = Field(min_length=1, max_length=50)
    category: Literal["normal", "negative", "permission", "regression", "journey", "exploratory"]
    environment: Literal["isolated_api", "isolated_browser", "isolated_fullstack"]
    runtime: QARuntime | None = None
    # Exact approved text, keyed by acceptance id. Execution adaptation never edits it.
    approved_oracles: dict[Identity, Text]


class QAScript(QAAsset):
    steps: list[Step] = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def has_behavior_assertion(self) -> Self:
        browser_seen = False
        for step in self.steps:
            if isinstance(step, BrowserStep):
                browser_seen = True
            elif browser_seen:
                raise ValueError("API 步骤须在浏览器步骤之前")
        if not any(
            (isinstance(step, APIStep) and step.assertions)
            or (isinstance(step, BrowserStep) and step.action.startswith("expect_"))
            for step in self.steps
        ):
            raise ValueError("独立测试必须包含业务断言，不能只有状态码或页面加载")
        return self


class QASuite(QAAsset):
    schema_version: Literal[1] = 1
    revision: int = Field(ge=1)
    scenarios: list[QAScenario] = Field(min_length=1, max_length=200)
    scripts: dict[Identity, QAScript] = Field(default_factory=dict, max_length=200)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        ids = [case.test_id for case in self.scenarios]
        if len(ids) != len(set(ids)) or not set(self.scripts) <= set(ids):
            raise ValueError("测试 id 重复或脚本没有关联场景")
        return self

    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        # Preserve exact hashes for suites written before scenario runtimes existed.
        for case in payload["scenarios"]:
            if case.get("runtime") is None:
                case.pop("runtime", None)
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()


def assertion_contract(script: QAScript) -> list[dict[str, Any]]:
    """Only endpoints/selectors/capture paths can change in an adapter repair.

    Input data, action order and observable assertions remain stable. Changes to
    these require an explicit challenge, never an automatic script repair.
    """
    contract = []
    for step in script.steps:
        if isinstance(step, BrowserStep) and step.action == "wait_for_visible":
            # A bounded visibility barrier changes no input, operation or business assertion.
            continue
        raw = step.model_dump(mode="json")
        adaptable = ("path", "capture") if isinstance(step, APIStep) else ("selector",)
        if isinstance(step, BrowserStep) and step.action == "navigate":
            adaptable = (*adaptable, "path")
        for name in adaptable:
            raw.pop(name, None)
        contract.append(raw)
    return contract
