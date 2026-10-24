"""Platform validation/freezing of QA drafts; no generated-source write access."""

from __future__ import annotations

from app.core.exceptions import BusinessException
from app.schemas.acceptance_test_plan import AcceptanceTestPlan
from app.schemas.app_spec import AppSpec
from app.schemas.qa_suite import QASuite, assertion_contract


def validate_suite(
    suite: QASuite,
    spec: AppSpec,
    plan: AcceptanceTestPlan | None,
    previous: QASuite | None = None,
) -> None:
    if len(suite.model_dump_json().encode("utf-8")) > 512 * 1024:
        raise BusinessException("测试资产超过 512 KiB 预算")
    criteria = {item.id: item for item in spec.acceptance_criteria}
    covered: set[str] = set()
    cases = {case.test_id: case for case in suite.scenarios}
    for case in suite.scenarios:
        ids = set(case.acceptance_ids)
        if len(ids) != len(case.acceptance_ids) or not ids <= criteria.keys():
            raise BusinessException("测试场景引用重复或未获批的验收条件")
        if case.approved_oracles != {key: criteria[key].text for key in case.acceptance_ids}:
            raise BusinessException("场景必须保留准确的获批预期；标准冲突须 challenge")
        covered |= ids
        script = suite.scripts.get(case.test_id)
        if script is not None:
            kinds = {step.kind for step in script.steps}
            if (case.environment == "isolated_api" and "browser" in kinds) or (
                case.environment == "isolated_browser" and "api" in kinds
            ):
                raise BusinessException("测试脚本执行方式与场景环境不一致")
    if covered != criteria.keys():
        raise BusinessException("测试场景必须覆盖全部获批验收条件")
    features = {feature.id for feature in spec.features}
    covered_features = {key for ac in covered for key in criteria[ac].source_ids} & features
    if covered_features != features:
        raise BusinessException("获批功能缺少关联验收条件，须先解决产品标准缺口")
    if plan is not None:
        for test in plan.tests:
            if (
                test.test_id not in cases
                or cases[test.test_id].acceptance_ids != test.acceptance_ids
            ):
                raise BusinessException("QA 场景不能删除或替换冻结测试身份及验收映射")
    if previous is not None:
        for old in previous.scenarios:
            if cases.get(old.test_id) != old:
                raise BusinessException("不能删除场景或修改业务预期；请提交 test_challenges")
        for key, old_script in previous.scripts.items():
            new_script = suite.scripts.get(key)
            if new_script is None or assertion_contract(new_script) != assertion_contract(
                old_script
            ):
                raise BusinessException("不能删除操作或降低断言；请提交 test_challenges")


def suite_test_results(suite: QASuite, execution: dict) -> list[dict]:
    """Join runner facts to scenario identities, never infer facts from requirements."""
    results = {row["test_id"]: row for row in execution.get("results", [])}
    return [
        {
            "test_id": case.test_id,
            "acceptance_ids": case.acceptance_ids,
            "check_id": "acceptance",
            "status": results.get(case.test_id, {}).get("status", "not_run"),
            "command": "run_acceptance_tests",
            "evidence": results.get(case.test_id, {}).get("evidence", "未执行独立测试"),
            "artifact_paths": [],
            "execution_id": execution.get("execution_id"),
            "failure_kind": results.get(case.test_id, {}).get("failure_kind"),
        }
        for case in suite.scenarios
    ]


RUNNER_VERSION = "forgeai-qa-v2"
LEGACY_RUNNER_VERSIONS = frozenset({"forgeai-qa-v1"})


def validate_manifest(
    manifest: dict,
    identity: dict,
    suite: QASuite,
    *,
    allow_legacy: bool = False,
) -> None:
    if not isinstance(manifest, dict):
        raise BusinessException("执行器清单格式无效")
    if any(manifest.get(key) != value for key, value in identity.items()):
        raise BusinessException("独立执行证据身份不匹配")
    accepted_versions = (
        {RUNNER_VERSION, *LEGACY_RUNNER_VERSIONS} if allow_legacy else {RUNNER_VERSION}
    )
    if manifest.get("runner_version") not in accepted_versions:
        raise BusinessException("独立执行器版本不匹配")
    rows = manifest.get("results")
    expected = {case.test_id for case in suite.scenarios}
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise BusinessException("执行器缺少逐项测试证据")
    if {row.get("test_id") for row in rows if isinstance(row, dict)} != expected:
        raise BusinessException("执行器测试身份不匹配")
    for row in rows:
        if row.get("status") not in {"passed", "failed", "blocked", "not_run"}:
            raise BusinessException("执行器状态无效")
        if not isinstance(row.get("evidence"), str) or not row["evidence"]:
            raise BusinessException("执行器证据缺失")
        kinds: dict[str, set[str | None]] = {
            "passed": {None},
            "failed": {"product"},
            "blocked": {"script", "environment", "conflict"},
            "not_run": {"script"},
        }
        if row.get("failure_kind") not in kinds[row["status"]]:
            raise BusinessException("执行器失败分类与状态不一致")
        observations = row.get("observations")
        if not isinstance(observations, list) or any(
            not isinstance(item, dict) for item in observations
        ):
            raise BusinessException("逐步观察格式无效")
        if row["status"] == "passed":
            script = suite.scripts.get(row["test_id"])
            if script is None or len(observations) != len(script.steps):
                raise BusinessException("未完整执行脚本不能通过")
