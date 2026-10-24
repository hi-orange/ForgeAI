"""QA verification state, recovery and publication gate, independent of tool dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.exceptions import BusinessException
from app.schemas.acceptance_test_plan import AcceptanceTestPlan
from app.schemas.agent_action import ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.qa_suite import QASuite
from app.schemas.system_design import SystemDesign
from app.schemas.test_report import (
    DefectRecord,
    TestReport,
    TestReportDecision,
    TestVerification,
    VerificationStatus,
)
from app.services.qa_assets import (
    RUNNER_VERSION,
    suite_test_results,
    validate_manifest,
    validate_suite,
)

QUALITY_PROGRESS_SCHEMA_VERSION = 6
BASE_QA_TOOL_TURNS = 64
MANUAL_QA_EXTRA_TURNS = 64
MAX_MANUAL_QA_GRANTS = 3
BASE_QA_EXECUTIONS = 6
MANUAL_QA_EXTRA_EXECUTIONS = 2
MAX_MANUAL_QA_EXECUTION_GRANTS = 3
MAX_CHECKPOINT_OUTPUT_CHARS = 8_000
_NON_REUSABLE_CHECK_ERRORS = frozenset(
    {
        "CHECK_ENVIRONMENT_UNAVAILABLE",
        "CHECK_TIMEOUT",
        "SOURCE_CHANGED",
        "SOURCE_MISMATCH",
    }
)


class DefectResolution(BaseModel):
    """Audit of a QA defect superseded by an exact independent passing run."""

    model_config = ConfigDict(extra="forbid")

    defect_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    suite_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_id: str = Field(min_length=1, max_length=64)
    runner_version: str = Field(min_length=1, max_length=64)


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
    defect_resolutions: dict[str, DefectResolution] = field(default_factory=dict)
    suite_versions: list[QASuite] = field(default_factory=list)
    executions: list[dict[str, Any]] = field(default_factory=list)
    tool_turns: int = 0
    last_qa_progress_turn: int = 0
    budget_extension_attempts: list[int] = field(default_factory=list)
    execution_extension_attempts: list[int] = field(default_factory=list)
    thinking_enabled: bool = False

    @property
    def tool_budget(self) -> int:
        return BASE_QA_TOOL_TURNS + MANUAL_QA_EXTRA_TURNS * len(self.budget_extension_attempts)

    @property
    def budget_extension_attempt(self) -> int | None:
        """Latest grant id kept for readers of legacy QA checkpoints."""

        return self.budget_extension_attempts[-1] if self.budget_extension_attempts else None

    @property
    def execution_budget(self) -> int:
        return BASE_QA_EXECUTIONS + MANUAL_QA_EXTRA_EXECUTIONS * len(
            self.execution_extension_attempts
        )

    @property
    def active_defects(self) -> dict[str, DefectRecord]:
        return {
            defect_id: defect
            for defect_id, defect in self.defects.items()
            if defect_id not in self.defect_resolutions
        }


def is_reusable_check(result: ToolExecutionResult) -> bool:
    if result.name in {"run_check", "capture_screenshots"}:
        from app.tools.checks import CHECK_RUNTIME_VERSION

        if result.data.get("runtime_version") != CHECK_RUNTIME_VERSION:
            return False
    output = result.data.get("output")
    stale_visual_runtime = result.name == "capture_screenshots" and (
        isinstance(output, str) and "Unknown check" in output
    )
    return result.error_code not in _NON_REUSABLE_CHECK_ERRORS and not stale_visual_runtime


def qa_execution_budget_exhausted(progress: object) -> bool:
    """Project the spent execution allowance without changing the checkpoint."""

    if not isinstance(progress, dict):
        return False
    grants = progress.get("execution_extension_attempts", [])
    executions = progress.get("executions")
    return (
        isinstance(grants, list)
        and isinstance(executions, list)
        and len(executions) == BASE_QA_EXECUTIONS + MANUAL_QA_EXTRA_EXECUTIONS * len(grants)
    )


def can_extend_qa_execution_budget(progress: object) -> bool:
    """Preflight an explicit retry; restore still validates the full checkpoint."""

    if not isinstance(progress, dict) or not qa_execution_budget_exhausted(progress):
        return False
    grants = progress.get("execution_extension_attempts", [])
    return isinstance(grants, list) and len(grants) < MAX_MANUAL_QA_EXECUTION_GRANTS


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
        "defect_resolutions": [
            resolution.model_dump(mode="json") for resolution in state.defect_resolutions.values()
        ],
        "suite_versions": [suite.model_dump(mode="json") for suite in state.suite_versions],
        "executions": state.executions,
        "tool_turns": state.tool_turns,
        "last_qa_progress_turn": state.last_qa_progress_turn,
        "budget_extension_attempt": state.budget_extension_attempt,
        "budget_extension_attempts": list(state.budget_extension_attempts),
        "execution_extension_attempts": list(state.execution_extension_attempts),
        "thinking_enabled": state.thinking_enabled,
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
        progress.get("schema_version") not in {1, 2, 3, 4, 5, QUALITY_PROGRESS_SCHEMA_VERSION}
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
        raw_execution_grants = progress.get("execution_extension_attempts", [])
        if (
            not isinstance(raw_execution_grants, list)
            or (progress["schema_version"] >= 6 and "execution_extension_attempts" not in progress)
            or (progress["schema_version"] < 6 and "execution_extension_attempts" in progress)
            or len(raw_execution_grants) > MAX_MANUAL_QA_EXECUTION_GRANTS
            or any(type(attempt) is not int or attempt < 2 for attempt in raw_execution_grants)
            or any(
                previous >= current
                for previous, current in zip(
                    raw_execution_grants, raw_execution_grants[1:], strict=False
                )
            )
        ):
            raise BusinessException("QA 独立执行续额记录无效")
        execution_grants: list[int] = raw_execution_grants
        if (
            execution_grants[: len(state.execution_extension_attempts)]
            != state.execution_extension_attempts
        ):
            raise BusinessException("QA 独立执行续额记录不能倒退")
        raw_executions = progress.get("executions", [])
        minimum_spent_for_execution_grants = (
            BASE_QA_EXECUTIONS + MANUAL_QA_EXTRA_EXECUTIONS * (len(execution_grants) - 1)
            if execution_grants
            else 0
        )
        if (
            len(suites) > 32
            or not isinstance(raw_executions, list)
            or len(raw_executions) < minimum_spent_for_execution_grants
            or len(raw_executions)
            > BASE_QA_EXECUTIONS + MANUAL_QA_EXTRA_EXECUTIONS * len(execution_grants)
        ):
            raise BusinessException("检查点超过 QA 预算")
        for index, suite in enumerate(suites):
            validate_suite(
                suite,
                state.app_spec,
                state.acceptance_test_plan,
                suites[index - 1] if index else inherited,
            )
        executions = []
        for manifest in raw_executions:
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
                allow_legacy=True,
            )
            executions.append(manifest)
        execution_ids = [item["execution_id"] for item in executions]
        if len(execution_ids) != len(set(execution_ids)):
            raise BusinessException("QA 独立执行记录重复")
        if state.executions and executions[: len(state.executions)] != state.executions:
            raise BusinessException("QA 独立执行记录不能倒退")
        tool_turns = max(state.tool_turns, int(progress.get("tool_turns", 0)))
        latest_grant = progress.get("budget_extension_attempt")
        raw_grants = progress.get("budget_extension_attempts")
        if raw_grants is None and progress["schema_version"] < 4:
            raw_grants = [latest_grant] if latest_grant is not None else []
        if (
            not isinstance(raw_grants, list)
            or len(raw_grants) > MAX_MANUAL_QA_GRANTS
            or any(type(attempt) is not int or attempt < 2 for attempt in raw_grants)
            or any(
                previous >= current
                for previous, current in zip(raw_grants, raw_grants[1:], strict=False)
            )
            or latest_grant != (raw_grants[-1] if raw_grants else None)
        ):
            raise BusinessException("QA 预算扩额记录无效")
        grants: list[int] = raw_grants
        if grants[: len(state.budget_extension_attempts)] != state.budget_extension_attempts:
            raise BusinessException("QA 预算扩额记录不能倒退")
        minimum_spent_for_grants = (
            BASE_QA_TOOL_TURNS + MANUAL_QA_EXTRA_TURNS * (len(grants) - 1) if grants else 0
        )
        if (
            tool_turns < minimum_spent_for_grants
            or tool_turns > BASE_QA_TOOL_TURNS + MANUAL_QA_EXTRA_TURNS * len(grants)
        ):
            raise BusinessException("QA 工具预算记录无效")
        # Older checkpoints did not track inspection progress. Start counting
        # from recovery rather than retroactively treating earlier turns as reads.
        last_qa_progress_turn = progress.get("last_qa_progress_turn", tool_turns)
        if (
            type(last_qa_progress_turn) is not int
            or not 0 <= last_qa_progress_turn <= tool_turns
            or last_qa_progress_turn < state.last_qa_progress_turn
        ):
            raise BusinessException("QA 只读进度记录无效")
        thinking_enabled = progress.get("thinking_enabled")
        if thinking_enabled is not None and type(thinking_enabled) is not bool:
            raise BusinessException("QA 思考模式记录无效")
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
    state.last_qa_progress_turn = last_qa_progress_turn
    state.budget_extension_attempts = grants
    state.execution_extension_attempts = execution_grants
    if thinking_enabled is not None:
        state.thinking_enabled = thinking_enabled
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
    if progress["schema_version"] >= 5 and not isinstance(raw_defects, list):
        raise BusinessException("QA 缺陷审计记录损坏")
    if isinstance(raw_defects, list):
        loaded_defects: dict[str, DefectRecord] = {}
        for raw in raw_defects:
            try:
                defect = DefectRecord.model_validate(raw)
            except ValidationError as exc:
                if progress["schema_version"] >= 5:
                    raise BusinessException("QA 缺陷审计记录损坏") from exc
                continue
            if not set(defect.related_requirement_ids).issubset(requirement_ids):
                if progress["schema_version"] >= 5:
                    raise BusinessException("QA 缺陷审计记录损坏")
                continue
            if defect.defect_id in loaded_defects:
                raise BusinessException("QA 缺陷审计记录重复")
            loaded_defects[defect.defect_id] = defect
        if progress["schema_version"] >= 5:
            state.defects = loaded_defects
        else:
            state.defects.update(loaded_defects)
    if progress["schema_version"] >= 5 and "defect_resolutions" not in progress:
        raise BusinessException("QA 缺陷解决记录损坏")
    raw_resolutions = progress.get("defect_resolutions", [])
    if not isinstance(raw_resolutions, list) or len(raw_resolutions) > 100:
        raise BusinessException("QA 缺陷解决记录损坏")
    resolutions: dict[str, DefectResolution] = {}
    try:
        for raw in raw_resolutions:
            resolution = DefectResolution.model_validate(raw)
            if resolution.defect_id in resolutions:
                raise BusinessException("QA 缺陷解决记录重复")
            validate_defect_resolution(state, resolution)
            resolutions[resolution.defect_id] = resolution
    except (BusinessException, ValidationError) as exc:
        raise BusinessException("QA 缺陷解决记录损坏") from exc
    state.defect_resolutions = resolutions


def validate_defect_resolution(state: QAVerificationState, resolution: DefectResolution) -> None:
    """Verify saved resolution against the original runner record, not QA prose."""

    defect = state.defects.get(resolution.defect_id)
    suite = next(
        (item for item in state.suite_versions if item.digest() == resolution.suite_hash),
        None,
    )
    execution = next(
        (item for item in state.executions if item.get("execution_id") == resolution.execution_id),
        None,
    )
    if defect is None or suite is None or execution is None:
        raise BusinessException("缺陷解决缺少原始缺陷、套件或执行证据")
    original_index = next(
        (
            index
            for index, item in enumerate(state.executions)
            if item.get("execution_id") == defect.execution_id
        ),
        None,
    )
    resolved_index = state.executions.index(execution)
    if original_index is None or original_index >= resolved_index:
        raise BusinessException("缺陷解决没有晚于原始失败执行")
    original_rows = {
        row["test_id"]: row for row in state.executions[original_index].get("results", [])
    }
    if not defect.related_test_ids or any(
        original_rows.get(test_id, {}).get("status") != "failed"
        or original_rows[test_id].get("failure_kind") != "product"
        for test_id in defect.related_test_ids
    ):
        raise BusinessException("原始缺陷缺少业务失败证据")
    if execution.get("runner_version") != resolution.runner_version:
        raise BusinessException("缺陷解决执行器身份不匹配")
    validate_manifest(
        execution,
        {
            "execution_id": resolution.execution_id,
            "code_item_id": state.code_item_id,
            "source_hash": state.code_source_hash,
            "suite_hash": resolution.suite_hash,
        },
        suite,
        allow_legacy=True,
    )
    results = {row["test_id"]: row["status"] for row in execution["results"]}
    if not defect.related_test_ids or any(
        results.get(test_id) != "passed" for test_id in defect.related_test_ids
    ):
        raise BusinessException("关联用例未全部通过，缺陷不能解决")


def unresolved_script_blockers(state: QAVerificationState) -> list[str]:
    """Known script failures still using the identical script in the current suite."""

    if not state.suite_versions:
        return []
    current = state.suite_versions[-1]
    previous_execution = next(
        (
            item
            for item in reversed(state.executions)
            if item.get("runner_version") == RUNNER_VERSION
            and item.get("code_item_id") == state.code_item_id
            and item.get("source_hash") == state.code_source_hash
        ),
        None,
    )
    if previous_execution is None:
        return []
    previous = next(
        (
            item
            for item in reversed(state.suite_versions)
            if item.digest() == previous_execution.get("suite_hash")
        ),
        None,
    )
    if previous is None or previous.digest() == current.digest():
        return []
    return sorted(
        row["test_id"]
        for row in previous_execution["results"]
        if row.get("failure_kind") == "script"
        and row.get("status") in {"blocked", "not_run"}
        and previous.scripts.get(row["test_id"]) == current.scripts.get(row["test_id"])
    )


def resolvable_defect_ids(state: QAVerificationState) -> list[str]:
    """Active defects whose linked cases passed the latest exact QA execution."""

    if not state.suite_versions or not state.executions or not state.active_defects:
        return []
    suite = state.suite_versions[-1]
    execution = state.executions[-1]
    if execution.get("runner_version") != RUNNER_VERSION:
        return []
    try:
        validate_manifest(
            execution,
            {
                "execution_id": execution["execution_id"],
                "code_item_id": state.code_item_id,
                "source_hash": state.code_source_hash,
                "suite_hash": suite.digest(),
            },
            suite,
        )
    except (BusinessException, KeyError):
        return []
    statuses = {row["test_id"]: row["status"] for row in execution["results"]}
    return sorted(
        defect_id
        for defect_id, defect in state.active_defects.items()
        if defect.related_test_ids
        and all(statuses.get(test_id) == "passed" for test_id in defect.related_test_ids)
    )


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


def build_test_report_from_decision(
    decision: TestReportDecision, state: QAVerificationState
) -> TestReport:
    """Attach authoritative suite, execution and check facts to a compact QA judgment."""

    if not state.suite_versions:
        if decision.suite_hash is not None or decision.execution_id is not None:
            raise BusinessException("标准争议报告不能引用不存在的测试执行")
        if decision.quality_conclusion.value != "blocked" or not decision.test_challenges:
            raise BusinessException("未设计独立套件时只能提交标准争议报告")
        planned = (
            [(test.test_id, test.acceptance_ids) for test in state.acceptance_test_plan.tests]
            if state.acceptance_test_plan is not None
            else [(f"test_{item.id}", [item.id]) for item in state.app_spec.acceptance_criteria]
        )
        report = TestReport.model_validate(
            {
                "code_item_id": state.code_item_id,
                "source_hash": state.code_source_hash,
                "test_plan_item_id": state.acceptance_test_plan_item_id,
                "test_hash": state.acceptance_test_hash,
                "requirement_results": [
                    {
                        "requirement_id": item.id,
                        "status": "not_run",
                        "evidence": "冻结验收标准争议待解决，尚未运行独立测试。",
                    }
                    for item in state.app_spec.acceptance_criteria
                ],
                "check_results": [
                    {
                        "check_id": "contract",
                        "status": "blocked",
                        "evidence": "冻结验收标准争议待解决。",
                    }
                ],
                "test_results": [
                    {
                        "test_id": test_id,
                        "acceptance_ids": acceptance_ids,
                        "check_id": "contract",
                        "status": "not_run",
                        "command": "review_acceptance_contract",
                        "evidence": "冻结验收标准争议待解决。",
                    }
                    for test_id, acceptance_ids in planned
                ],
                "test_challenges": decision.test_challenges,
                "quality_conclusion": decision.quality_conclusion,
                "summary": decision.summary,
                "residual_risks": decision.residual_risks,
            }
        )
        return validate_report(report, state)

    suite = state.suite_versions[-1]
    suite_hash = suite.digest()
    if decision.suite_hash != suite_hash:
        raise BusinessException("质量结论没有引用当前 suite_hash")
    execution = next(
        (item for item in reversed(state.executions) if item["suite_hash"] == suite_hash),
        None,
    )
    if decision.execution_id != (execution.get("execution_id") if execution else None):
        raise BusinessException("质量结论没有引用当前测试执行")
    actual_tests = suite_test_results(suite, execution or {})
    requirement_results = []
    for criterion in state.app_spec.acceptance_criteria:
        linked = [row for row in actual_tests if criterion.id in row["acceptance_ids"]]
        statuses = {row["status"] for row in linked}
        status = (
            "failed"
            if "failed" in statuses
            else "blocked"
            if "blocked" in statuses
            else "not_run"
            if not statuses or "not_run" in statuses
            else "passed"
        )
        requirement_results.append(
            {
                "requirement_id": criterion.id,
                "status": status,
                "evidence": (
                    f"独立执行 {decision.execution_id or '未执行'} 的关联用例 "
                    f"{', '.join(row['test_id'] for row in linked)} 状态为 {status}。"
                ),
            }
        )
    check_results = []
    for check_id, result in state.checks.items():
        evidence = f"平台检查 {check_id}，来源 {state.code_source_hash}。"
        if check_id.startswith("visual") and result.ok:
            screenshots = result.data.get("screenshots")
            if isinstance(screenshots, list):
                paths = [
                    str(item["path"])
                    for item in screenshots
                    if isinstance(item, dict) and isinstance(item.get("path"), str)
                ]
                evidence += " 截图：" + "、".join(paths[:2]) + "。"
        check_results.append(
            {
                "check_id": check_id,
                "status": check_status(result),
                "evidence": evidence,
            }
        )
    report = TestReport.model_validate(
        {
            "code_item_id": state.code_item_id,
            "source_hash": state.code_source_hash,
            "test_plan_item_id": state.acceptance_test_plan_item_id,
            "test_hash": state.acceptance_test_hash,
            "test_suite": suite,
            "suite_hash": suite_hash,
            "requirement_results": requirement_results,
            "check_results": check_results,
            "test_results": actual_tests,
            "regression_test_ids": [case.test_id for case in suite.scenarios],
            "defects": list(state.active_defects.values()),
            "test_challenges": decision.test_challenges,
            "quality_conclusion": decision.quality_conclusion,
            "summary": decision.summary,
            "residual_risks": decision.residual_risks,
        }
    )
    return validate_report(report, state)


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
        (
            item
            for item in reversed(state.executions)
            if item["suite_hash"] == suite.digest() and item.get("runner_version") == RUNNER_VERSION
        ),
        {},
    )
    if not execution:
        raise BusinessException("当前测试资产须使用最新独立执行器重新执行")
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
    active_defects = state.active_defects
    if set(reported_defects) != set(active_defects) or any(
        reported_defects[key] != value for key, value in active_defects.items()
    ):
        raise BusinessException("测试报告中的缺陷必须来自本轮独立记录")
    if (
        any(item.status == VerificationStatus.FAILED for item in report.requirement_results)
        and not report.defects
        and not report.test_challenges
    ):
        raise BusinessException("验收失败时必须记录至少一个缺陷")
    return report
