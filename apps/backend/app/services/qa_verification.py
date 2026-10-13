"""QA verification state, recovery and publication gate, independent of tool dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.exceptions import BusinessException
from app.schemas.acceptance_test_plan import AcceptanceTestPlan
from app.schemas.agent_action import ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.qa_suite import QASuite
from app.schemas.system_design import SystemDesign
from app.schemas.test_report import DefectRecord, TestReport, TestVerification, VerificationStatus
from app.services.qa_assets import suite_test_results, validate_manifest, validate_suite

QUALITY_PROGRESS_SCHEMA_VERSION = 2
MAX_CHECKPOINT_OUTPUT_CHARS = 8_000
_NON_REUSABLE_CHECK_ERRORS = frozenset(
    {
        "CHECK_ENVIRONMENT_UNAVAILABLE",
        "CHECK_TIMEOUT",
        "SOURCE_CHANGED",
        "SOURCE_MISMATCH",
    }
)


@dataclass(slots=True)
class QAVerificationState:
    app_spec: AppSpec
    code_item_id: str
    code_source_hash: str
    workspace_root: Path
    system_design: SystemDesign | None = None
    acceptance_test_plan_item_id: str | None = None
    acceptance_test_hash: str | None = None
    acceptance_test_plan: AcceptanceTestPlan | None = None
    checks: dict[str, ToolExecutionResult] = field(default_factory=dict)
    defects: dict[str, DefectRecord] = field(default_factory=dict)
    suite_versions: list[QASuite] = field(default_factory=list)
    executions: list[dict[str, Any]] = field(default_factory=list)
    tool_turns: int = 0


def is_reusable_check(result: ToolExecutionResult) -> bool:
    output = result.data.get("output")
    stale_visual_runtime = result.name == "capture_screenshots" and (
        isinstance(output, str) and "Unknown check" in output
    )
    return result.error_code not in _NON_REUSABLE_CHECK_ERRORS and not stale_visual_runtime


def export_test_engineer_progress(state: QAVerificationState) -> dict[str, Any]:
    """Create a bounded, source-bound checkpoint for a recoverable QA attempt."""

    checks: dict[str, dict[str, Any]] = {}
    for check_id, result in state.checks.items():
        payload = result.model_dump(mode="json")
        data = dict(payload.get("data") or {})
        output = data.get("output")
        if isinstance(output, str) and len(output) > MAX_CHECKPOINT_OUTPUT_CHARS:
            data["output"] = output[-MAX_CHECKPOINT_OUTPUT_CHARS:]
            payload["truncated"] = True
        payload["data"] = data
        checks[check_id] = payload
    return {
        "schema_version": QUALITY_PROGRESS_SCHEMA_VERSION,
        "code_item_id": state.code_item_id,
        "source_hash": state.code_source_hash,
        "test_plan_item_id": state.acceptance_test_plan_item_id,
        "test_hash": state.acceptance_test_hash,
        "checks": checks,
        "defects": [defect.model_dump(mode="json") for defect in state.defects.values()],
        "suite_versions": [suite.model_dump(mode="json") for suite in state.suite_versions],
        "executions": state.executions,
        "tool_turns": state.tool_turns,
    }


def restore_test_engineer_progress(
    state: QAVerificationState,
    progress: object,
    *,
    for_recovery: bool = True,
) -> None:
    """Read exact-source facts; recovery drops checks that must be rerun.

    Publication keeps unavailable/timeout observations to validate a blocked
    report against what actually happened in the finishing execution.
    """

    if not isinstance(progress, dict) or (
        progress.get("schema_version") not in {1, QUALITY_PROGRESS_SCHEMA_VERSION}
        or progress.get("code_item_id") != state.code_item_id
        or progress.get("source_hash") != state.code_source_hash
        or progress.get("test_plan_item_id") != state.acceptance_test_plan_item_id
        or progress.get("test_hash") != state.acceptance_test_hash
    ):
        return
    # Legacy check evidence remains readable, but cannot produce independent case passes.
    try:
        suites = [QASuite.model_validate(raw) for raw in progress.get("suite_versions", [])]
        if "suite_versions" not in progress:
            suites = list(state.suite_versions)
        inherited = state.suite_versions[0] if state.suite_versions else None
        if inherited is not None and not suites:
            raise BusinessException("检查点不能丢弃继承的回归套件")
        if len(suites) > 32 or len(progress.get("executions", [])) > 6:
            raise BusinessException("检查点超过 QA 预算")
        for index, suite in enumerate(suites):
            validate_suite(
                suite,
                state.app_spec,
                state.acceptance_test_plan,
                suites[index - 1] if index else inherited,
            )
        executions = []
        for manifest in progress.get("executions", []):
            suite = next(s for s in suites if s.digest() == manifest.get("suite_hash"))
            validate_manifest(
                manifest,
                {
                    "execution_id": manifest["execution_id"],
                    "code_item_id": state.code_item_id,
                    "source_hash": state.code_source_hash,
                    "suite_hash": suite.digest(),
                },
                suite,
            )
            executions.append(manifest)
        tool_turns = max(state.tool_turns, int(progress.get("tool_turns", 0)))
    except (
        ValidationError,
        BusinessException,
        ValueError,
        KeyError,
        StopIteration,
        TypeError,
    ) as exc:
        raise BusinessException("QA 检查点损坏；不能丢弃既有测试或重置执行预算") from exc
    state.suite_versions = suites
    state.executions = executions
    state.tool_turns = tool_turns
    raw_checks = progress.get("checks")
    if isinstance(raw_checks, dict):
        for check_id, raw in raw_checks.items():
            if not isinstance(check_id, str) or not isinstance(raw, dict):
                continue
            try:
                result = ToolExecutionResult.model_validate(raw)
            except ValidationError:
                continue
            if for_recovery and not is_reusable_check(result):
                continue
            observed_hash = result.data.get("source_hash")
            if observed_hash != state.code_source_hash:
                continue
            state.checks[check_id] = result
    requirement_ids = {item.id for item in state.app_spec.acceptance_criteria}
    raw_defects = progress.get("defects")
    if isinstance(raw_defects, list):
        for raw in raw_defects:
            try:
                defect = DefectRecord.model_validate(raw)
            except ValidationError:
                continue
            if set(defect.related_requirement_ids).issubset(requirement_ids):
                state.defects[defect.defect_id] = defect


def check_status(result: ToolExecutionResult) -> VerificationStatus:
    if result.ok:
        return VerificationStatus.PASSED
    manifest = result.data.get("manifest")
    if result.name == "run_acceptance_tests" and isinstance(manifest, dict):
        return (
            VerificationStatus.FAILED
            if any(row["status"] == "failed" for row in manifest["results"])
            else VerificationStatus.BLOCKED
        )
    if result.error_code in {
        "CHECK_ENVIRONMENT_UNAVAILABLE",
        "CHECK_TIMEOUT",
        "SOURCE_CHANGED",
        "SOURCE_MISMATCH",
    }:
        return VerificationStatus.BLOCKED
    return VerificationStatus.FAILED


def validate_report(report: TestReport, state: QAVerificationState) -> TestReport:
    if (report.code_item_id, report.source_hash) != (
        state.code_item_id,
        state.code_source_hash,
    ):
        raise BusinessException("测试报告引用的不是本轮准确代码结果")
    if (report.test_plan_item_id, report.test_hash) != (
        state.acceptance_test_plan_item_id,
        state.acceptance_test_hash,
    ):
        raise BusinessException("测试报告引用的不是本轮冻结验收标准")
    if not state.suite_versions:
        # A standard conflict can be escalated before executable coverage exists.
        # These are explicitly unexecuted cases, never manufactured runtime facts.
        planned = (
            {test.test_id: test.acceptance_ids for test in state.acceptance_test_plan.tests}
            if state.acceptance_test_plan is not None
            else {f"test_{item.id}": [item.id] for item in state.app_spec.acceptance_criteria}
        )
        if (
            report.test_challenges
            and report.quality_conclusion.value == "blocked"
            and report.test_suite is None
            and not report.defects
            and {row.test_id: row.acceptance_ids for row in report.test_results} == planned
            and all(
                row.status == VerificationStatus.NOT_RUN
                and row.execution_id is None
                and row.check_id == "contract"
                and row.command == "review_acceptance_contract"
                and not row.artifact_paths
                for row in report.test_results
            )
            and {row.requirement_id for row in report.requirement_results}
            == {item.id for item in state.app_spec.acceptance_criteria}
            and all(row.status == VerificationStatus.NOT_RUN for row in report.requirement_results)
            and len(report.check_results) == 1
            and report.check_results[0].check_id == "contract"
            and report.check_results[0].status == VerificationStatus.BLOCKED
        ):
            return report
        raise BusinessException("报告必须包含平台冻结的独立测试场景，不能从需求结论合成测试结果")
    suite = state.suite_versions[-1]
    if report.test_suite != suite or report.suite_hash != suite.digest():
        raise BusinessException("测试报告没有绑定当前独立测试资产版本")
    execution = next(
        (item for item in reversed(state.executions) if item["suite_hash"] == suite.digest()), {}
    )
    actual_tests = [
        TestVerification.model_validate(row) for row in suite_test_results(suite, execution)
    ]
    if report.test_results != actual_tests:
        raise BusinessException("逐项测试结果必须准确引用运行器证据；smoke、截图和模型结论不能替代")
    if report.quality_conclusion.value == "passed" and set(report.regression_test_ids) != {
        case.test_id for case in suite.scenarios
    }:
        raise BusinessException("交付前必须在当前源码和测试版本完成全套回归")
    for requirement in report.requirement_results:
        linked = [
            item.status
            for item in actual_tests
            if requirement.requirement_id in item.acceptance_ids
        ]
        expected_status = (
            VerificationStatus.FAILED
            if VerificationStatus.FAILED in linked
            else VerificationStatus.BLOCKED
            if VerificationStatus.BLOCKED in linked
            else VerificationStatus.NOT_RUN
            if not linked or VerificationStatus.NOT_RUN in linked
            else VerificationStatus.PASSED
        )
        if requirement.status != expected_status:
            raise BusinessException("需求结论必须来自关联独立用例的实际状态")
    acceptance_ids = {item.id for item in state.app_spec.acceptance_criteria}
    reported_ids = {item.requirement_id for item in report.requirement_results}
    if reported_ids != acceptance_ids:
        raise BusinessException("测试报告必须逐条覆盖 PRD 验收条件")
    if "all" not in state.checks:
        raise BusinessException("提交测试报告前必须运行完整 all 检查")
    visual_check_ids = [check_id for check_id in state.checks if check_id.startswith("visual")]
    if not visual_check_ids:
        raise BusinessException("提交测试报告前必须生成桌面端与移动端截图")
    reported_checks = {item.check_id: item for item in report.check_results}
    if set(reported_checks) != set(state.checks):
        raise BusinessException("测试报告必须记录本轮全部实际检查")
    for check_id, result in state.checks.items():
        if reported_checks[check_id].status != check_status(result):
            raise BusinessException(f"检查 {check_id} 的报告状态与实际结果不一致")
    for check_id in visual_check_ids:
        visual_result = state.checks[check_id]
        if visual_result.ok:
            raw_screenshots = visual_result.data.get("screenshots")
            screenshots = raw_screenshots if isinstance(raw_screenshots, list) else []
            paths = [
                str(item.get("path"))
                for item in screenshots
                if isinstance(item, dict) and item.get("path")
            ]
            visual_evidence = reported_checks[check_id].evidence
            if len(paths) != 2 or any(path not in visual_evidence for path in paths):
                raise BusinessException(f"检查 {check_id} 的证据必须引用本轮桌面端与移动端截图路径")
    reported_defects = {item.defect_id: item for item in report.defects}
    if set(reported_defects) != set(state.defects) or any(
        reported_defects[key] != value for key, value in state.defects.items()
    ):
        raise BusinessException("测试报告中的缺陷必须来自本轮独立记录")
    if (
        any(item.status == VerificationStatus.FAILED for item in report.requirement_results)
        and not report.defects
        and not report.test_challenges
    ):
        raise BusinessException("验收失败时必须记录至少一个缺陷")
    return report
