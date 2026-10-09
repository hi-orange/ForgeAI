"""Create and resolve immutable acceptance-test plans before coding begins."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException
from app.models.configuration_item import ConfigurationItem, ConfigurationItemType
from app.schemas.acceptance_test_plan import (
    ACCEPTANCE_TEST_PLAN_SCHEMA_VERSION,
    AcceptanceTestCase,
    AcceptanceTestPlan,
    TestLayer,
)
from app.schemas.configuration_item import ConfigurationItemRegistration
from app.services import configuration_manager

if TYPE_CHECKING:
    from app.services.engineering.handoff import EngineeringSource


def _test_layer(text: str) -> tuple[TestLayer, str, str, str]:
    """Map intent to evidence the platform can really collect.

    Earlier plans named pytest/vitest/playwright files that were never materialized.
    Those fictional paths made every QA run fail before it could judge the app.
    The frozen plan now points at platform-owned runtime and browser evidence
    channels that Test Engineer can actually invoke.
    """
    normalized = text.lower()
    if any(token in normalized for token in ("接口", "api", "http", "请求", "响应")):
        return (
            TestLayer.API,
            "forgeai-runtime",
            "platform://fullstack/runtime",
            'run_check(check_id="all")',
        )
    if any(token in normalized for token in ("计算", "规则", "权限", "校验", "状态")):
        return (
            TestLayer.BACKEND,
            "forgeai-runtime",
            "platform://fullstack/runtime",
            'run_check(check_id="all")',
        )
    if any(token in normalized for token in ("组件", "表单", "按钮", "显示", "渲染")):
        return (
            TestLayer.FRONTEND,
            "forgeai-browser",
            "platform://browser/runtime",
            'run_check(check_id="all") + capture_screenshots(route="/")',
        )
    return (
        TestLayer.E2E,
        "forgeai-browser",
        "platform://browser/runtime",
        'run_check(check_id="all") + capture_screenshots(route="/")',
    )


def build_acceptance_test_plan(source: EngineeringSource) -> AcceptanceTestPlan:
    """Compile approved behavior into stable test identities without reading code."""

    tests: list[AcceptanceTestCase] = []
    for criterion in source.app_spec.acceptance_criteria:
        layer, framework, target_path, command = _test_layer(criterion.text)
        tests.append(
            AcceptanceTestCase(
                test_id=f"test_{criterion.id}",
                acceptance_ids=[criterion.id],
                layer=layer,
                framework=framework,
                target_path=target_path,
                command=command,
                objective=f"验证获批验收条件 {criterion.id}",
                oracle=criterion.text,
            )
        )
    return AcceptanceTestPlan(
        source_item_id=source.input_item.item_id,
        tests=tests,
        protected_paths=[],
        regression_policy=(
            "首次交付执行全部测试；修复轮优先执行失败测试及受修改文件影响的同层测试，"
            "发布结论前仍须执行完整回归。"
        ),
    )


def get_or_create_acceptance_test_plan(
    db: Session,
    *,
    project_id: int,
    run_id: str,
    source: EngineeringSource,
) -> ConfigurationItem:
    """Return the one frozen plan for an exact approved engineering source."""

    existing = db.scalar(
        select(ConfigurationItem)
        .where(
            ConfigurationItem.project_id == project_id,
            ConfigurationItem.producer_run_id == run_id,
            ConfigurationItem.semantic_type == ConfigurationItemType.ACCEPTANCE_TEST_PLAN.value,
            ConfigurationItem.state == "usable",
        )
        .order_by(ConfigurationItem.version.desc())
    )
    if existing is not None:
        try:
            plan = AcceptanceTestPlan.model_validate(existing.payload)
        except Exception as exc:
            raise ConflictException("已冻结的验收测试计划正文无效") from exc
        if plan.source_item_id == source.input_item.item_id:
            return existing

    plan = build_acceptance_test_plan(source)
    return configuration_manager.stage_configuration_item(
        db,
        project_id=project_id,
        producer_run_id=run_id,
        submission=ConfigurationItemRegistration(
            semantic_type=ConfigurationItemType.ACCEPTANCE_TEST_PLAN,
            schema_version=ACCEPTANCE_TEST_PLAN_SCHEMA_VERSION,
            payload=plan.model_dump(mode="json"),
            upstream_item_ids=[source.input_item.item_id],
        ),
    )


def load_acceptance_test_plan(
    db: Session,
    *,
    project_id: int,
    run_id: str,
    item_id: str,
    source_item_id: str,
    lock: bool = False,
) -> tuple[ConfigurationItem, AcceptanceTestPlan]:
    statement = select(ConfigurationItem).where(
        ConfigurationItem.item_id == item_id,
        ConfigurationItem.project_id == project_id,
        ConfigurationItem.producer_run_id == run_id,
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    item = db.scalar(statement)
    if (
        item is None
        or item.semantic_type != ConfigurationItemType.ACCEPTANCE_TEST_PLAN.value
        or item.state != "usable"
        or item.upstream_item_ids != [source_item_id]
    ):
        raise ConflictException("工程任务引用了无效或来源不一致的冻结验收测试计划")
    try:
        plan = AcceptanceTestPlan.model_validate(item.payload)
    except Exception as exc:
        raise ConflictException("冻结验收测试计划正文无效") from exc
    if plan.source_item_id != source_item_id:
        raise ConflictException("冻结验收测试计划与工程来源身份不一致")
    return item, plan
