"""Independent QA contracts and real fail/repair/retest Docker regression."""

import copy
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import ValidationError
from qa_demo import create_demo, demo_spec, demo_suite
from qa_fixtures import add_unit_evidence, unit_manifest, unit_suite
from test_architect import valid_spec
from test_test_engineer import passed_report, visual_result

from app.core.exceptions import BusinessException
from app.schemas.agent_action import ToolCall
from app.schemas.qa_suite import QAScript, QASuite, assertion_contract
from app.schemas.test_report import TestReport as Report
from app.services.acceptance_testing import build_acceptance_test_plan
from app.services.qa_assets import suite_test_results, validate_manifest, validate_suite
from app.services.qa_verification import QAVerificationState as QAState
from app.services.qa_verification import (
    can_extend_qa_execution_budget,
    check_status,
    export_test_engineer_progress,
    resolvable_defect_ids,
    restore_test_engineer_progress,
)
from app.tools.checks import CHECK_RUNTIME_VERSION, source_snapshot
from app.tools.test_engineer import execute_test_engineer_tool


def call(state, name, **arguments):
    return execute_test_engineer_tool(ToolCall(id=name, name=name, arguments=arguments), state)


def report_for(state):
    suite = state.suite_versions[-1]
    results = suite_test_results(suite, state.executions[-1])
    requirements = []
    for criterion in state.app_spec.acceptance_criteria:
        statuses = {row["status"] for row in results if criterion.id in row["acceptance_ids"]}
        status = next(
            value for value in ["failed", "blocked", "not_run", "passed"] if value in statuses
        )
        requirements.append(
            {"requirement_id": criterion.id, "status": status, "evidence": "关联独立用例执行结果"}
        )
    checks = []
    for key, result in state.checks.items():
        detail = result.summary
        if key.startswith("visual") and result.ok:
            detail += " " + " ".join(item["path"] for item in result.data["screenshots"])
        checks.append({"check_id": key, "status": check_status(result).value, "evidence": detail})
    failed = any(row["status"] == "failed" for row in [*requirements, *checks])
    passed = all(row["status"] == "passed" for row in [*requirements, *checks])
    return {
        "code_item_id": state.code_item_id,
        "source_hash": state.code_source_hash,
        "test_plan_item_id": state.acceptance_test_plan_item_id,
        "test_hash": state.acceptance_test_hash,
        "test_suite": suite.model_dump(mode="json"),
        "suite_hash": suite.digest(),
        "requirement_results": requirements,
        "check_results": checks,
        "test_results": results,
        "regression_test_ids": [case.test_id for case in suite.scenarios],
        "defects": [item.model_dump(mode="json") for item in state.active_defects.values()],
        "quality_conclusion": "failed" if failed else "passed" if passed else "blocked",
        "summary": "独立 QA 真实执行与回归验收",
        "residual_risks": [],
    }


def two_case_suite():
    payload = unit_suite().model_dump(mode="json")
    second = copy.deepcopy(payload["scenarios"][0])
    second["test_id"] = "test_ac_track_second"
    payload["scenarios"].append(second)
    payload["scripts"]["test_ac_track_second"] = copy.deepcopy(payload["scripts"]["test_ac_track"])
    return QASuite.model_validate(payload)


class IndependentQAContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(TemporaryDirectory())
        self.state = QAState(
            app_spec=valid_spec(),
            code_item_id="ci_code",
            code_source_hash="a" * 64,
            workspace_root=Path(self.directory),
        )

    def test_design_coverage_oracles_and_no_source_write(self):
        before = source_snapshot(self.state.workspace_root)[1]
        suite = unit_suite()
        result, _ = call(self.state, "design_test_cases", suite=suite.model_dump(mode="json"))
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(source_snapshot(self.state.workspace_root)[1], before)
        changed = suite.model_dump(mode="json")
        changed["scenarios"][0]["approved_oracles"]["ac_track"] = "health 返回 200"
        result, _ = call(self.state, "design_test_cases", suite=changed)
        self.assertFalse(result.ok)
        unknown = suite.model_dump(mode="json")
        unknown["scenarios"][0]["acceptance_ids"] = ["unapproved"]
        self.assertFalse(call(self.state, "design_test_cases", suite=unknown)[0].ok)
        spec = valid_spec().model_copy(
            update={
                "features": [
                    *valid_spec().features,
                    valid_spec().features[0].model_copy(update={"id": "feat_uncovered"}),
                ]
            }
        )
        with self.assertRaises(BusinessException):
            validate_suite(suite, spec, None)

    def test_adapter_revision_keeps_old_snapshot_and_rejects_weakening(self):
        suite = unit_suite()
        call(self.state, "design_test_cases", suite=suite.model_dump(mode="json"))
        script = suite.scripts["test_ac_track"].model_dump(mode="json")
        script["steps"][0]["path"] = "/api/v2/records"
        result, _ = call(
            self.state,
            "write_test_script",
            test_id="test_ac_track",
            expected_suite_hash=suite.digest(),
            script=script,
        )
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(self.state.suite_versions[0], suite)
        self.assertNotEqual(result.data["suite_hash"], suite.digest())
        self.assertFalse(
            call(
                self.state,
                "write_test_script",
                test_id="test_ac_track",
                expected_suite_hash=suite.digest(),
                script=script,
            )[0].ok
        )
        script["steps"][0]["assertions"][0]["operator"] = "exists"
        result, _ = call(
            self.state,
            "write_test_script",
            test_id="test_ac_track",
            expected_suite_hash=self.state.suite_versions[-1].digest(),
            script=script,
        )
        self.assertFalse(result.ok)
        self.assertIn("断言", result.summary)

    def test_visibility_barrier_can_be_inserted_without_changing_approved_script(self):
        raw = demo_suite().model_dump(mode="json")
        original_steps = raw["scripts"]["test_ac_track"]["steps"]
        original_steps[:] = [
            step for step in original_steps if step.get("action") != "wait_for_visible"
        ]
        original = QASuite.model_validate(raw)
        amended = copy.deepcopy(raw)
        amended["revision"] += 1
        steps = amended["scripts"]["test_ac_track"]["steps"]
        login_click = next(
            index
            for index, step in enumerate(steps)
            if step.get("selector") == '[data-testid="login"]'
        )
        steps.insert(
            login_click + 1,
            {
                "kind": "browser",
                "action": "wait_for_visible",
                "selector": "[data-testid='identity']",
            },
        )
        synchronized = QASuite.model_validate(amended)
        validate_suite(synchronized, demo_spec(), None, original)
        self.assertEqual(
            assertion_contract(synchronized.scripts["test_ac_track"]),
            assertion_contract(original.scripts["test_ac_track"]),
        )
        validate_suite(original, demo_spec(), None, synchronized)

        weakened = copy.deepcopy(amended)
        weakened["scripts"]["test_ac_track"]["steps"] = [
            step
            for step in weakened["scripts"]["test_ac_track"]["steps"]
            if not (
                step.get("action") == "expect_text" and step.get("value") == "Signed in as alice"
            )
        ]
        with self.assertRaisesRegex(BusinessException, "不能删除操作"):
            validate_suite(QASuite.model_validate(weakened), demo_spec(), None, original)

    def test_protocol_rejects_arbitrary_code_and_assertion_free_script(self):
        for script in (
            {"steps": [{"kind": "shell", "command": "true"}]},
            {"steps": [{"kind": "api", "path": "/health", "expect_status": 200}]},
        ):
            with self.assertRaises(ValidationError):
                QAScript.model_validate(script)
        QAScript.model_validate(
            {
                "steps": [
                    {"kind": "browser", "action": "fill", "selector": "input", "value": ""},
                    {"kind": "browser", "action": "expect_text", "value": "Required"},
                ]
            }
        )
        for script in (
            {"steps": [{"kind": "browser", "action": "wait_for_visible"}]},
            {
                "steps": [
                    {"kind": "browser", "action": "wait_for_visible", "selector": "main"},
                ]
            },
            {
                "steps": [
                    {
                        "kind": "browser",
                        "action": "wait_for_visible",
                        "selector": "main",
                        "value": "passed",
                    },
                    {"kind": "browser", "action": "expect_text", "value": "passed"},
                ]
            },
        ):
            with self.assertRaises(ValidationError):
                QAScript.model_validate(script)

    def test_legacy_runner_manifest_is_readable_but_cannot_publish_new_report(self):
        suite = unit_suite()
        legacy = unit_manifest(suite)
        legacy["runner_version"] = "forgeai-qa-v1"
        identity = {
            key: legacy[key]
            for key in ("execution_id", "code_item_id", "source_hash", "suite_hash")
        }
        with self.assertRaisesRegex(BusinessException, "执行器版本"):
            validate_manifest(legacy, identity, suite)
        validate_manifest(legacy, identity, suite, allow_legacy=True)
        self.state.suite_versions = [suite]
        self.state.executions = [legacy]
        restored = QAState(
            app_spec=valid_spec(),
            code_item_id="ci_code",
            code_source_hash="a" * 64,
            workspace_root=self.state.workspace_root,
        )
        restore_test_engineer_progress(restored, export_test_engineer_progress(self.state))
        self.assertEqual(restored.executions, [legacy])
        result, _ = call(restored, "write_test_report", test_report=passed_report())
        self.assertFalse(result.ok)
        self.assertIn("最新独立执行器", result.summary)

    def test_evidence_rejects_stale_forged_missing_case_and_incomplete_execution(self):
        suite = unit_suite()
        manifest = unit_manifest(suite)
        identity = {
            key: manifest[key]
            for key in ("execution_id", "code_item_id", "source_hash", "suite_hash")
        }
        for mutate in (
            lambda item: item.update(source_hash="b" * 64),
            lambda item: item["results"][0].update(test_id="invented"),
            lambda item: item.update(results=[]),
            lambda item: item["results"][0].update(observations=[]),
        ):
            forged = copy.deepcopy(manifest)
            mutate(forged)
            with self.assertRaises(BusinessException):
                validate_manifest(forged, identity, suite)

    def test_report_cannot_claim_unexecuted_case_or_use_smoke_as_evidence(self):
        suite = unit_suite()
        self.state.suite_versions = [suite]
        self.state.checks["visual"] = visual_result()
        report = passed_report()
        result, _ = call(self.state, "write_test_report", test_report=report)
        self.assertFalse(result.ok)
        self.assertIn("重新执行", result.summary)
        add_unit_evidence(self.state)
        report["test_results"][0]["check_id"] = "all"
        self.assertFalse(call(self.state, "write_test_report", test_report=report)[0].ok)

    def test_recovery_preserves_assets_history_budget_and_fences_other_code(self):
        add_unit_evidence(self.state)
        self.state.tool_turns = 37
        saved = export_test_engineer_progress(self.state)
        restored = QAState(
            app_spec=valid_spec(),
            code_item_id="ci_code",
            code_source_hash="a" * 64,
            workspace_root=Path(self.directory),
        )
        restore_test_engineer_progress(restored, saved)
        self.assertEqual(restored.tool_turns, 37)
        self.assertEqual(restored.executions, self.state.executions)
        restore_test_engineer_progress(restored, saved)
        self.assertEqual(restored.executions, self.state.executions)
        repaired = QAState(
            app_spec=valid_spec(),
            code_item_id="ci_new",
            code_source_hash="b" * 64,
            workspace_root=Path(self.directory),
            suite_versions=[unit_suite()],
        )
        restore_test_engineer_progress(repaired, saved)
        self.assertEqual(repaired.executions, [])
        self.assertEqual(repaired.suite_versions, [unit_suite()])

    def test_corrupt_checkpoint_cannot_discard_assertions_and_reset_budget(self):
        add_unit_evidence(self.state)
        saved = export_test_engineer_progress(self.state)
        saved["suite_versions"][0]["scripts"] = {}
        with self.assertRaisesRegex(BusinessException, "检查点损坏"):
            restore_test_engineer_progress(self.state, saved)
        self.assertEqual(self.state.suite_versions, [unit_suite()])
        self.assertEqual(len(self.state.executions), 1)

    def test_same_suite_can_rerun_after_environment_recovery_without_rewriting_assets(self):
        self.state.suite_versions = [unit_suite()]
        from app.schemas.agent_action import ToolExecutionResult

        blocked = unit_manifest(status="blocked")
        blocked["results"][0]["failure_kind"] = "environment"
        passed = unit_manifest()
        passed["execution_id"] = "qa_recovered_unit_fixture"
        with patch(
            "app.tools.test_engineer.qa_runner.run_acceptance_tests",
            side_effect=[
                ToolExecutionResult(
                    tool_call_id="first",
                    name="run_acceptance_tests",
                    ok=False,
                    summary="offline",
                    data={"manifest": blocked},
                ),
                ToolExecutionResult(
                    tool_call_id="second",
                    name="run_acceptance_tests",
                    ok=True,
                    summary="recovered",
                    data={"manifest": passed},
                ),
            ],
        ) as runner:
            self.assertFalse(call(self.state, "run_acceptance_tests")[0].ok)
            self.assertTrue(call(self.state, "run_acceptance_tests")[0].ok)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(self.state.executions, [blocked, passed])
        self.assertEqual(self.state.suite_versions, [unit_suite()])

    def test_same_suite_can_rerun_when_one_case_passed_and_another_was_environment_blocked(self):
        from app.schemas.agent_action import ToolExecutionResult

        suite = two_case_suite()
        self.state.suite_versions = [suite]
        partial = unit_manifest(suite)
        partial["results"][1].update(status="blocked", failure_kind="environment")
        recovered = unit_manifest(suite)
        recovered["execution_id"] = "qa_recovered_mixed_fixture"
        with patch(
            "app.tools.test_engineer.qa_runner.run_acceptance_tests",
            side_effect=[
                ToolExecutionResult(
                    tool_call_id="first",
                    name="run_acceptance_tests",
                    ok=False,
                    summary="one case was unavailable",
                    data={"manifest": partial},
                ),
                ToolExecutionResult(
                    tool_call_id="second",
                    name="run_acceptance_tests",
                    ok=True,
                    summary="environment recovered",
                    data={"manifest": recovered},
                ),
            ],
        ) as runner:
            self.assertFalse(call(self.state, "run_acceptance_tests")[0].ok)
            self.assertTrue(call(self.state, "run_acceptance_tests")[0].ok)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(self.state.executions, [partial, recovered])

    def test_same_suite_still_rejects_rerun_after_product_failure_with_environment_block(self):
        suite = two_case_suite()
        self.state.suite_versions = [suite]
        mixed = unit_manifest(suite, "failed")
        mixed["results"][1].update(status="blocked", failure_kind="environment")
        self.state.executions = [mixed]
        with patch("app.tools.test_engineer.qa_runner.run_acceptance_tests") as runner:
            result, _ = call(self.state, "run_acceptance_tests")
        self.assertFalse(result.ok)
        self.assertIn("不能重复启动同一快照", result.summary)
        runner.assert_not_called()

    def test_defect_resolution_requires_latest_full_pass_and_preserves_audit(self):
        from app.schemas.agent_action import ToolExecutionResult

        suite = two_case_suite()
        self.state.suite_versions = [suite]
        failed = unit_manifest(suite, "failed")
        self.state.executions = [failed]
        self.assertEqual(resolvable_defect_ids(self.state), [])
        defect = {
            "defect_id": "reading_flow_failure",
            "severity": "major",
            "title": "阅读流程失败",
            "description": "两个独立路径均未满足验收预期。",
            "evidence": "独立执行中的两个用例均为 product 失败。",
            "related_requirement_ids": ["ac_track"],
            "related_test_ids": ["test_ac_track", "test_ac_track_second"],
            "execution_id": failed["execution_id"],
        }
        self.assertTrue(call(self.state, "record_defect", defect=defect)[0].ok)
        self.assertEqual(set(self.state.active_defects), {defect["defect_id"]})
        original_hash = suite.digest()
        unresolved_args = {
            "defect_id": defect["defect_id"],
            "suite_hash": original_hash,
            "execution_id": failed["execution_id"],
        }
        self.assertFalse(call(self.state, "resolve_defect", **unresolved_args)[0].ok)
        self.assertTrue(
            call(
                self.state,
                "adapt_test_script",
                test_id="test_ac_track",
                expected_suite_hash=original_hash,
                step_index=0,
                path="/api/v2/records",
            )[0].ok
        )
        current_suite = self.state.suite_versions[-1]
        self.assertFalse(
            call(
                self.state,
                "resolve_defect",
                defect_id=defect["defect_id"],
                suite_hash=current_suite.digest(),
                execution_id=failed["execution_id"],
            )[0].ok
        )
        partial = unit_manifest(current_suite, source_hash=self.state.code_source_hash)
        partial["execution_id"] = "qa_partial"
        partial["results"][1].update(status="failed", failure_kind="product")
        self.state.executions.append(partial)
        self.assertEqual(resolvable_defect_ids(self.state), [])
        self.assertFalse(
            call(
                self.state,
                "resolve_defect",
                defect_id=defect["defect_id"],
                suite_hash=current_suite.digest(),
                execution_id="qa_partial",
            )[0].ok
        )
        passed = unit_manifest(current_suite, source_hash=self.state.code_source_hash)
        passed["execution_id"] = "qa_full_pass"
        self.state.executions.append(passed)
        self.assertEqual(resolvable_defect_ids(self.state), [defect["defect_id"]])
        self.assertFalse(
            call(
                self.state,
                "resolve_defect",
                defect_id=defect["defect_id"],
                suite_hash=original_hash,
                execution_id="qa_full_pass",
            )[0].ok
        )
        resolution_args = {
            "defect_id": defect["defect_id"],
            "suite_hash": current_suite.digest(),
            "execution_id": "qa_full_pass",
        }
        result, _ = call(self.state, "resolve_defect", **resolution_args)
        self.assertTrue(result.ok, result.summary)
        self.assertFalse(self.state.active_defects)
        self.assertEqual(resolvable_defect_ids(self.state), [])
        self.assertIn(defect["defect_id"], self.state.defects)
        self.assertFalse(call(self.state, "resolve_defect", **resolution_args)[0].ok)
        saved = export_test_engineer_progress(self.state)
        self.assertEqual(len(saved["defects"]), 1)
        self.assertEqual(len(saved["defect_resolutions"]), 1)
        restored = QAState(
            app_spec=valid_spec(),
            code_item_id="ci_code",
            code_source_hash="a" * 64,
            workspace_root=self.state.workspace_root,
        )
        restore_test_engineer_progress(restored, saved)
        self.assertEqual(restored.defects, self.state.defects)
        self.assertEqual(restored.defect_resolutions, self.state.defect_resolutions)
        self.assertFalse(restored.active_defects)
        tampered = copy.deepcopy(saved)
        tampered["defect_resolutions"][0]["execution_id"] = failed["execution_id"]
        with self.assertRaisesRegex(BusinessException, "缺陷解决记录损坏"):
            restore_test_engineer_progress(
                QAState(
                    app_spec=valid_spec(),
                    code_item_id="ci_code",
                    code_source_hash="a" * 64,
                    workspace_root=self.state.workspace_root,
                ),
                tampered,
            )
        erased_audit = copy.deepcopy(saved)
        del erased_audit["defects"]
        with self.assertRaisesRegex(BusinessException, "缺陷审计记录损坏"):
            restore_test_engineer_progress(
                QAState(
                    app_spec=valid_spec(),
                    code_item_id="ci_code",
                    code_source_hash="a" * 64,
                    workspace_root=self.state.workspace_root,
                ),
                erased_audit,
            )
        self.state.checks["all"] = ToolExecutionResult(
            tool_call_id="all",
            name="run_check",
            ok=True,
            summary="all passed",
            data={"check_id": "all", "source_hash": self.state.code_source_hash},
        )
        self.state.checks["visual"] = visual_result()
        decision = {
            "suite_hash": current_suite.digest(),
            "execution_id": "qa_full_pass",
            "quality_conclusion": "passed",
            "summary": "最新独立复测及完整检查通过，先前缺陷已经解决。",
        }
        result, report = call(self.state, "write_test_report", report_decision=decision)
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(report.defects, [])

    def test_rerun_rejects_unchanged_script_blockers_without_spending_execution(self):
        from app.schemas.agent_action import ToolExecutionResult

        suite = two_case_suite()
        self.state.suite_versions = [suite]
        blocked = unit_manifest(suite, "blocked")
        for row in blocked["results"]:
            row["failure_kind"] = "script"
        self.state.executions = [blocked]
        self.assertTrue(
            call(
                self.state,
                "adapt_test_script",
                test_id="test_ac_track",
                expected_suite_hash=suite.digest(),
                step_index=0,
                path="/api/v2/records",
            )[0].ok
        )
        with patch("app.tools.test_engineer.qa_runner.run_acceptance_tests") as runner:
            result, _ = call(self.state, "run_acceptance_tests")
            self.assertFalse(result.ok)
            self.assertIn("test_ac_track_second", result.summary)
            self.assertEqual(len(self.state.executions), 1)
            runner.assert_not_called()
            result, _ = call(
                self.state,
                "run_acceptance_tests",
                override_reason="final_blocked_evidence",
            )
            self.assertFalse(result.ok)
            runner.assert_not_called()
        current_hash = self.state.suite_versions[-1].digest()
        self.assertTrue(
            call(
                self.state,
                "adapt_test_script",
                test_id="test_ac_track_second",
                expected_suite_hash=current_hash,
                step_index=0,
                path="/api/v3/records",
            )[0].ok
        )
        final_suite = self.state.suite_versions[-1]
        passed = unit_manifest(final_suite, source_hash=self.state.code_source_hash)
        passed["execution_id"] = "qa_after_all_script_edits"
        with patch(
            "app.tools.test_engineer.qa_runner.run_acceptance_tests",
            return_value=ToolExecutionResult(
                tool_call_id="rerun",
                name="run_acceptance_tests",
                ok=True,
                summary="all passed",
                data={"manifest": passed},
            ),
        ) as runner:
            result, _ = call(self.state, "run_acceptance_tests")
            self.assertTrue(result.ok, result.summary)
            runner.assert_called_once()
        self.assertEqual(len(self.state.executions), 2)

    def test_terminal_repair_inherits_only_exact_baseline_suite(self):
        from app.services import test_engineer as service

        db = MagicMock()
        unrelated = passed_report()
        unrelated["code_item_id"] = "ci_other"
        db.scalars.return_value = [
            SimpleNamespace(payload=unrelated),
            SimpleNamespace(payload=passed_report()),
        ]
        revision = SimpleNamespace(
            kind="implementation_repair", source_run_id="run_old", baseline_code_item_id="ci_code"
        )
        with patch.object(
            service.revision_service, "get_revision_for_target", return_value=revision
        ):
            self.assertEqual(service._revision_suite(db, 1, "run_new"), unit_suite())
            revision.kind = "product_change"
            db.reset_mock()
            self.assertIsNone(service._revision_suite(db, 1, "run_new"))
            db.scalars.assert_not_called()
            revision.kind = "implementation_repair"
            revision.baseline_code_item_id = "ci_missing"
            self.assertIsNone(service._revision_suite(db, 1, "run_new"))

    def test_standard_conflict_can_be_reported_before_execution(self):
        payload = passed_report()
        payload.pop("test_suite")
        payload.pop("suite_hash")
        payload["quality_conclusion"] = "blocked"
        payload["requirement_results"][0]["status"] = "not_run"
        payload["check_results"] = [
            {"check_id": "contract", "status": "blocked", "evidence": "标准冲突，等待复核"}
        ]
        for row in payload["test_results"]:
            row.update(
                status="not_run",
                check_id="contract",
                command="review_acceptance_contract",
                execution_id=None,
                evidence="标准冲突，尚未执行",
            )
        payload["test_challenges"] = [
            {
                "challenge_id": "conflict",
                "test_ids": ["test_ac_track"],
                "reason": "批准的可观察预期存在歧义",
                "requested_resolution": "请确认保存后的可见范围",
            }
        ]
        result, report = call(self.state, "write_test_report", test_report=payload)
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(report.quality_conclusion.value, "blocked")
        payload["test_results"][0]["status"] = "passed"
        self.assertFalse(call(self.state, "write_test_report", test_report=payload)[0].ok)

    def test_model_budget_is_reserved_before_call_and_survives_provider_failure(self):
        from app.agents.test_engineer import verify_code

        self.state.tool_turns = 63
        saved = []
        with patch("app.agents.test_engineer.chat_with_tools", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                verify_code(
                    spec=valid_spec(),
                    code_item_id="ci_code",
                    code_source_hash="a" * 64,
                    workspace_root=self.state.workspace_root,
                    system_design=None,
                    saved_progress=export_test_engineer_progress(self.state),
                    on_progress=saved.append,
                )
        self.assertEqual(saved[-1]["tool_turns"], 64)
        with patch("app.agents.test_engineer.chat_with_tools") as model:
            with self.assertRaises(BusinessException):
                verify_code(
                    spec=valid_spec(),
                    code_item_id="ci_code",
                    code_source_hash="a" * 64,
                    workspace_root=self.state.workspace_root,
                    system_design=None,
                    saved_progress=saved[-1],
                )
        model.assert_not_called()

    def test_explicit_qa_budget_extension_keeps_identity_evidence_and_thinking(self):
        from app.agents.test_engineer import verify_code
        from app.schemas.agent_action import ToolExecutionResult

        self.state.tool_turns = 64
        self.state.checks["all"] = ToolExecutionResult(
            tool_call_id="all",
            name="run_check",
            ok=True,
            summary="完整检查通过",
            data={
                "check_id": "all",
                "source_hash": "a" * 64,
                "runtime_version": CHECK_RUNTIME_VERSION,
            },
        )
        previous = export_test_engineer_progress(self.state)
        # The live failed execution predates thinking-mode persistence.
        previous["schema_version"] = 2
        previous.pop("execution_extension_attempts")
        previous.pop("thinking_enabled")
        previous.pop("budget_extension_attempt")
        previous.pop("budget_extension_attempts")
        saved = []
        with (
            patch("app.agents.test_engineer.settings.test_engineer_thinking", True),
            patch(
                "app.agents.test_engineer.chat_with_tools", side_effect=RuntimeError("offline")
            ) as model,
            self.assertRaisesRegex(RuntimeError, "offline"),
        ):
            verify_code(
                spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash="a" * 64,
                workspace_root=self.state.workspace_root,
                system_design=None,
                saved_progress=previous,
                manual_budget_extension_attempt=4,
                on_progress=saved.append,
            )
        self.assertEqual(saved[0]["budget_extension_attempt"], 4)
        self.assertEqual(saved[0]["budget_extension_attempts"], [4])
        self.assertEqual(saved[0]["tool_turns"], 64)
        self.assertTrue(saved[0]["thinking_enabled"])
        self.assertEqual(saved[-1]["tool_turns"], 65)
        self.assertIn("all", saved[-1]["checks"])
        self.assertTrue(model.call_args.kwargs["thinking"])
        self.assertTrue(model.call_args.kwargs["parallel_tool_calls"])

        restored = QAState(
            app_spec=valid_spec(),
            code_item_id="ci_code",
            code_source_hash="a" * 64,
            workspace_root=self.state.workspace_root,
        )
        restore_test_engineer_progress(restored, saved[-1])
        self.assertEqual(restored.tool_budget, 128)
        self.assertEqual(restored.budget_extension_attempt, 4)
        self.assertEqual(restored.budget_extension_attempts, [4])
        self.assertTrue(restored.thinking_enabled)
        self.assertIn("all", restored.checks)
        with self.assertRaisesRegex(BusinessException, "再次扩额"):
            verify_code(
                spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash="a" * 64,
                workspace_root=self.state.workspace_root,
                system_design=None,
                saved_progress=saved[-1],
                manual_budget_extension_attempt=5,
            )

    def test_three_explicit_qa_grants_preserve_spent_turns_and_stop_at_cap(self):
        from app.agents.test_engineer import verify_code

        self.state.tool_turns = 64
        progress = export_test_engineer_progress(self.state)
        for attempt, old_budget, new_budget in ((4, 64, 128), (6, 128, 192), (8, 192, 256)):
            self.assertEqual(progress["tool_turns"], old_budget)
            saved = []
            with (
                patch(
                    "app.agents.test_engineer.chat_with_tools",
                    side_effect=RuntimeError("offline"),
                ),
                self.assertRaisesRegex(RuntimeError, "offline"),
            ):
                verify_code(
                    spec=valid_spec(),
                    code_item_id="ci_code",
                    code_source_hash="a" * 64,
                    workspace_root=self.state.workspace_root,
                    system_design=None,
                    saved_progress=progress,
                    manual_budget_extension_attempt=attempt,
                    on_progress=saved.append,
                )
            self.assertEqual(saved[0]["tool_turns"], old_budget)
            self.assertEqual(saved[0]["budget_extension_attempts"][-1], attempt)
            self.assertEqual(saved[-1]["tool_turns"], old_budget + 1)
            progress = saved[-1]
            progress["tool_turns"] = new_budget
            restored = QAState(
                app_spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash="a" * 64,
                workspace_root=self.state.workspace_root,
            )
            restore_test_engineer_progress(restored, progress)
            self.assertEqual(restored.tool_budget, new_budget)
            self.assertEqual(restored.tool_turns, new_budget)
        self.assertEqual(progress["budget_extension_attempts"], [4, 6, 8])
        with self.assertRaisesRegex(BusinessException, "再次扩额"):
            verify_code(
                spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash="a" * 64,
                workspace_root=self.state.workspace_root,
                system_design=None,
                saved_progress=progress,
                manual_budget_extension_attempt=9,
            )

    def test_legacy_v3_qa_grant_restores_without_repeating_first_grant(self):
        self.state.tool_turns = 118
        self.state.budget_extension_attempts = [4]
        saved = export_test_engineer_progress(self.state)
        saved["schema_version"] = 3
        saved.pop("execution_extension_attempts")
        saved.pop("budget_extension_attempts")
        restored = QAState(
            app_spec=valid_spec(),
            code_item_id="ci_code",
            code_source_hash="a" * 64,
            workspace_root=self.state.workspace_root,
        )
        restore_test_engineer_progress(restored, saved)
        self.assertEqual(restored.tool_turns, 118)
        self.assertEqual(restored.tool_budget, 128)
        self.assertEqual(restored.budget_extension_attempts, [4])
        upgraded = export_test_engineer_progress(restored)
        self.assertEqual(upgraded["schema_version"], 6)
        self.assertEqual(upgraded["budget_extension_attempts"], [4])

    def test_execution_budget_extension_is_explicit_source_bound_and_auditable(self):
        from app.agents.test_engineer import verify_code
        from app.schemas.agent_action import ToolExecutionResult

        suite = unit_suite()
        self.state.suite_versions = [suite]
        for index in range(6):
            manifest = unit_manifest(suite)
            manifest["execution_id"] = f"qa_previous_{index}"
            self.state.executions.append(manifest)
        self.assertEqual(self.state.execution_budget, 6)
        self.assertTrue(can_extend_qa_execution_budget(export_test_engineer_progress(self.state)))
        result, _ = call(
            self.state,
            "adapt_test_script",
            test_id="test_ac_track",
            expected_suite_hash=suite.digest(),
            step_index=0,
            path="/api/v2/records",
        )
        self.assertTrue(result.ok, result.summary)
        current_suite = self.state.suite_versions[-1]
        with patch("app.tools.test_engineer.qa_runner.run_acceptance_tests") as runner:
            result, _ = call(self.state, "run_acceptance_tests")
        self.assertFalse(result.ok)
        self.assertIn("执行预算已用尽", result.summary)
        runner.assert_not_called()

        progress = export_test_engineer_progress(self.state)
        progress["schema_version"] = 5
        progress.pop("execution_extension_attempts")
        saved = []
        with (
            patch("app.agents.test_engineer.chat_with_tools", side_effect=RuntimeError("offline")),
            self.assertRaisesRegex(RuntimeError, "offline"),
        ):
            verify_code(
                spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash="a" * 64,
                workspace_root=self.state.workspace_root,
                system_design=None,
                saved_progress=progress,
                manual_execution_extension_attempt=4,
                on_progress=saved.append,
            )
        self.assertEqual(saved[0]["execution_extension_attempts"], [4])
        self.assertEqual(saved[0]["executions"], progress["executions"])
        self.assertEqual(saved[0]["suite_versions"], progress["suite_versions"])
        with self.assertRaisesRegex(BusinessException, "再次追加独立执行次数"):
            verify_code(
                spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash="b" * 64,
                workspace_root=self.state.workspace_root,
                system_design=None,
                saved_progress=progress,
                manual_execution_extension_attempt=4,
            )

        restored = QAState(
            app_spec=valid_spec(),
            code_item_id="ci_code",
            code_source_hash="a" * 64,
            workspace_root=self.state.workspace_root,
        )
        restore_test_engineer_progress(restored, saved[-1])
        self.assertEqual(restored.execution_budget, 8)
        self.assertEqual(len(restored.executions), 6)
        self.assertEqual(restored.suite_versions[-1].digest(), current_suite.digest())
        seventh = unit_manifest(current_suite)
        seventh["execution_id"] = "qa_after_explicit_grant"
        with patch(
            "app.tools.test_engineer.qa_runner.run_acceptance_tests",
            return_value=ToolExecutionResult(
                tool_call_id="granted",
                name="run_acceptance_tests",
                ok=True,
                summary="复验通过",
                data={"manifest": seventh},
            ),
        ) as runner:
            result, _ = call(restored, "run_acceptance_tests")
        self.assertTrue(result.ok, result.summary)
        runner.assert_called_once()
        self.assertEqual(restored.executions[-1]["execution_id"], "qa_after_explicit_grant")
        self.assertEqual(len(restored.executions), 7)

    def test_execution_grants_cannot_be_forged_rewound_or_reissued(self):
        from app.agents.test_engineer import verify_code

        suite = unit_suite()
        self.state.suite_versions = [suite]
        for index in range(6):
            manifest = unit_manifest(suite)
            manifest["execution_id"] = f"qa_previous_{index}"
            self.state.executions.append(manifest)
        baseline = export_test_engineer_progress(self.state)
        for grants, count in (([4], 5), ([4, 4], 6), ([True], 6), ([4], 9)):
            with self.subTest(grants=grants, count=count):
                invalid = copy.deepcopy(baseline)
                invalid["execution_extension_attempts"] = grants
                invalid["executions"] = invalid["executions"][:count]
                if count == 9:
                    invalid["executions"].extend(copy.deepcopy(baseline["executions"][:3]))
                with self.assertRaisesRegex(BusinessException, "检查点损坏"):
                    restore_test_engineer_progress(
                        QAState(
                            app_spec=valid_spec(),
                            code_item_id="ci_code",
                            code_source_hash="a" * 64,
                            workspace_root=self.state.workspace_root,
                        ),
                        invalid,
                    )
        legacy = copy.deepcopy(baseline)
        legacy["schema_version"] = 5
        legacy.pop("execution_extension_attempts")
        restored = QAState(
            app_spec=valid_spec(),
            code_item_id="ci_code",
            code_source_hash="a" * 64,
            workspace_root=self.state.workspace_root,
        )
        restore_test_engineer_progress(restored, legacy)
        self.assertEqual(restored.execution_budget, 6)
        restored.execution_extension_attempts = [4]
        with self.assertRaisesRegex(BusinessException, "检查点损坏"):
            restore_test_engineer_progress(restored, baseline)
        capped = copy.deepcopy(baseline)
        capped["execution_extension_attempts"] = [4, 6, 8]
        for index in range(6, 12):
            manifest = unit_manifest(suite)
            manifest["execution_id"] = f"qa_previous_{index}"
            capped["executions"].append(manifest)
        self.assertFalse(can_extend_qa_execution_budget(capped))
        with self.assertRaisesRegex(BusinessException, "再次追加独立执行次数"):
            verify_code(
                spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash="a" * 64,
                workspace_root=self.state.workspace_root,
                system_design=None,
                saved_progress=capped,
                manual_execution_extension_attempt=10,
            )
        from app.core.exceptions import ConflictException
        from app.orchestration.test_engineer import run_test_engineer_task

        previous = SimpleNamespace(
            task_id="task_quality",
            status="failed",
            draft={"checkpoint": {"kind": "quality_checkpoint", "verification": capped}},
        )
        db = MagicMock()
        db.get.return_value = previous
        with self.assertRaisesRegex(ConflictException, "显式续额上限"):
            run_test_engineer_task(
                db,
                MagicMock(),
                1,
                "run_quality",
                "task_quality",
                recovery_execution_id="exec_prior",
                allow_execution_budget_extension=True,
            )

    def test_qa_grant_history_rejects_duplicate_and_corrupt_attempt_ids(self):
        from app.agents.test_engineer import verify_code

        self.state.tool_turns = 128
        self.state.budget_extension_attempts = [4]
        progress = export_test_engineer_progress(self.state)
        for attempts, latest in (([4, 4], 4), ([6, 4], 4), ([4, True], True), ([4, 6], 4)):
            invalid = copy.deepcopy(progress)
            invalid["budget_extension_attempts"] = attempts
            invalid["budget_extension_attempt"] = latest
            with self.assertRaisesRegex(BusinessException, "检查点损坏"):
                restore_test_engineer_progress(
                    QAState(
                        app_spec=valid_spec(),
                        code_item_id="ci_code",
                        code_source_hash="a" * 64,
                        workspace_root=self.state.workspace_root,
                    ),
                    invalid,
                )
        premature = copy.deepcopy(progress)
        premature["budget_extension_attempts"] = [4, 6]
        premature["budget_extension_attempt"] = 6
        premature["tool_turns"] = 127
        with self.assertRaisesRegex(BusinessException, "检查点损坏"):
            restore_test_engineer_progress(
                QAState(
                    app_spec=valid_spec(),
                    code_item_id="ci_code",
                    code_source_hash="a" * 64,
                    workspace_root=self.state.workspace_root,
                ),
                premature,
            )
        with self.assertRaisesRegex(BusinessException, "再次扩额"):
            verify_code(
                spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash="a" * 64,
                workspace_root=self.state.workspace_root,
                system_design=None,
                saved_progress=progress,
                manual_budget_extension_attempt=4,
            )

    def test_qa_budget_extension_rejects_different_source(self):
        from app.agents.test_engineer import verify_code

        self.state.tool_turns = 64
        saved = export_test_engineer_progress(self.state)
        with self.assertRaisesRegex(BusinessException, "不允许再次扩额"):
            verify_code(
                spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash="b" * 64,
                workspace_root=self.state.workspace_root,
                system_design=None,
                saved_progress=saved,
                manual_budget_extension_attempt=2,
            )

    def test_publication_rejects_pass_without_persisted_platform_evidence(self):
        from app.services import test_engineer as service

        db = MagicMock()
        db.get.return_value = None
        inputs = SimpleNamespace(
            code_item=SimpleNamespace(item_id="ci_code"),
            code_artifact=SimpleNamespace(source_hash="a" * 64),
            acceptance_test_plan_item=None,
            acceptance_test_plan=None,
            app_spec=valid_spec(),
        )
        with (
            patch.object(
                service.task_execution, "lock_run", return_value=SimpleNamespace(stage="qa")
            ),
            patch.object(
                service,
                "_load_task",
                return_value=(SimpleNamespace(status="running"), SimpleNamespace(status="running")),
            ),
            patch.object(service, "_validate_task", return_value="ci_code"),
            patch.object(service, "load_test_inputs", return_value=inputs),
            patch.object(
                service.task_execution, "require_execution", return_value=SimpleNamespace(draft={})
            ),
            self.assertRaisesRegex(BusinessException, "独立测试场景"),
        ):
            service.complete_test_engineer_task(
                db, MagicMock(), 1, "run", "task", "exec", Report.model_validate(passed_report())
            )
        db.rollback.assert_called_once()

    def test_publication_with_persisted_checkpoint_closes_real_sqlite_workflow(self):
        self._exercise_publication(blocked=False)

    def test_blocked_publication_preserves_retry_assets_and_never_reopens_terminal_run(self):
        self._exercise_publication(blocked=True)

    def test_failed_report_dispatch_replays_after_repair_worker_claims_task(self):
        self._exercise_publication(blocked=False, failed=True)

    def _exercise_publication(self, *, blocked, failed=False):
        # Database/lease/ownership integration only. These deliberately synthetic
        # observations are not the real application evidence from RealQADockerTests.
        from test_engineering_loop import EngineeringLoopTests

        from app.models.build_run import BuildRun
        from app.models.project import Project
        from app.orchestration.test_engineer import _save_quality_progress
        from app.schemas.agent_action import ToolExecutionResult
        from app.schemas.qa_suite import QASuite
        from app.services import leader
        from app.services import test_engineer as service
        from app.services.engineering.completion import complete_code_engineer_task

        workflow = EngineeringLoopTests()
        self.addCleanup(workflow.doCleanups)
        workflow.setUp()
        workflow._start()
        workflow._run()
        with workflow.session_factory() as db:
            code = complete_code_engineer_task(
                db,
                workflow.owner,
                workflow.project.id,
                workflow.run.run_id,
                workflow.task.task_id,
                workflow.execution.execution_id,
            )
            quality = leader.dispatch_completed_code(
                db, workflow.owner, workflow.project.id, workflow.run.run_id, code.item_id
            )
            _, execution = service.claim_test_engineer_task(
                db, workflow.owner, workflow.project.id, workflow.run.run_id, quality.task_id
            )
            inputs = service.load_test_inputs(
                db, workflow.project.id, workflow.run.run_id, code.item_id
            )
            cases = []
            for criterion in inputs.app_spec.acceptance_criteria:
                cases.append(
                    unit_suite()
                    .scenarios[0]
                    .model_copy(
                        update={
                            "test_id": f"test_{criterion.id}",
                            "acceptance_ids": [criterion.id],
                            "approved_oracles": {criterion.id: criterion.text},
                        }
                    )
                )
            suite = QASuite(
                scenarios=cases,
                revision=1,
                scripts={case.test_id: unit_suite().scripts["test_ac_track"] for case in cases},
            )
            state = QAState(
                app_spec=inputs.app_spec,
                code_item_id=code.item_id,
                code_source_hash=inputs.code_artifact.source_hash,
                workspace_root=workflow.root,
                acceptance_test_plan=inputs.acceptance_test_plan,
                acceptance_test_plan_item_id=inputs.acceptance_test_plan_item.item_id,
                acceptance_test_hash=inputs.acceptance_test_plan_item.content_hash,
                suite_versions=[suite],
            )
            manifest = unit_manifest(suite, source_hash=state.code_source_hash)
            manifest["code_item_id"] = code.item_id
            if failed:
                from app.schemas.test_report import DefectRecord

                row = manifest["results"][0]
                row.update(status="failed", failure_kind="product", evidence="synthetic failure")
                criterion_id = suite.scenarios[0].acceptance_ids[0]
                state.defects["unit_defect"] = DefectRecord(
                    defect_id="unit_defect",
                    severity="major",
                    title="Fixture failure",
                    description="Synthetic evidence for workflow state validation",
                    evidence="synthetic failure",
                    related_requirement_ids=[criterion_id],
                    related_test_ids=[row["test_id"]],
                    execution_id=manifest["execution_id"],
                )
            state.executions = [manifest]
            state.checks["all"] = ToolExecutionResult(
                tool_call_id="all",
                name="run_check",
                ok=True,
                summary="synthetic unit check",
                data={"source_hash": state.code_source_hash, "check_id": "all"},
            )
            state.checks["visual"] = visual_result()
            state.checks["visual"].data["source_hash"] = state.code_source_hash
            if blocked:
                state.checks["all"] = state.checks["all"].model_copy(
                    update={
                        "ok": False,
                        "error_code": "CHECK_TIMEOUT",
                        "summary": "synthetic timeout",
                    }
                )
            _save_quality_progress(db, execution, export_test_engineer_progress(state))
            report = Report.model_validate(report_for(state))
            published = service.complete_test_engineer_task(
                db,
                workflow.owner,
                workflow.project.id,
                workflow.run.run_id,
                quality.task_id,
                execution.execution_id,
                report,
            )
            self.assertEqual(published.schema_version, 3)
            self.assertEqual(published.payload["suite_hash"], suite.digest())
            run = db.get(BuildRun, workflow.run.id)
            self.assertEqual(run.status, "running" if blocked or failed else "succeeded")
            if not blocked and not failed:
                self.assertEqual(db.get(Project, workflow.project.id).status, "available")
            replay = service.complete_test_engineer_task(
                db,
                workflow.owner,
                workflow.project.id,
                workflow.run.run_id,
                quality.task_id,
                execution.execution_id,
                report,
            )
            self.assertEqual(replay.item_id, published.item_id)
            from app.core.exceptions import ConflictException
            from app.models.task_execution import TaskExecution
            from app.services.requirements import get_requirements_status
            from app.worker import _existing_result

            self.assertEqual(_existing_result(db, quality.task_id).item_id, published.item_id)
            self.assertEqual(_existing_result(db, workflow.task.task_id).item_id, code.item_id)
            dispatched = leader.dispatch_completed_code(
                db, workflow.owner, workflow.project.id, workflow.run.run_id, code.item_id
            )
            self.assertEqual(dispatched.task_id, quality.task_id)
            original_status = run.status
            code_replay = leader.dispatch_completed_design(
                db,
                workflow.owner,
                workflow.project.id,
                workflow.run.run_id,
                code.upstream_item_ids[0],
            )
            self.assertEqual(code_replay.task_id, workflow.task.task_id)
            self.assertEqual(run.status, original_status)
            if failed:
                from sqlalchemy import select

                from app.models.workflow_job import WorkflowJob
                from app.orchestration.leader import start_dispatched_delivery
                from app.services.engineering import claim_code_engineer_task

                repair = leader.dispatch_quality_repair(
                    db, workflow.owner, workflow.project.id, workflow.run.run_id, published.item_id
                )
                first_job = start_dispatched_delivery(
                    db, workflow.owner, workflow.project.id, workflow.run.run_id, repair
                )
                _, repair_execution = claim_code_engineer_task(
                    db, workflow.owner, workflow.project.id, workflow.run.run_id, repair.task_id
                )
                replay_repair = leader.dispatch_quality_repair(
                    db, workflow.owner, workflow.project.id, workflow.run.run_id, published.item_id
                )
                replay_job = start_dispatched_delivery(
                    db, workflow.owner, workflow.project.id, workflow.run.run_id, replay_repair
                )
                self.assertEqual(replay_repair.task_id, repair.task_id)
                self.assertEqual(first_job["job_id"], replay_job["job_id"])
                jobs = list(
                    db.scalars(select(WorkflowJob).where(WorkflowJob.task_id == repair.task_id))
                )
                self.assertEqual(len(jobs), 1)
                self.assertEqual(repair_execution.attempt, 1)
                self.assertEqual(run.stage, "developer")
            if blocked:
                status = get_requirements_status(db, workflow.owner, workflow.project.id)
                self.assertTrue(status.retryable)
                retried = leader.retry_blocked_quality_validation(
                    db, workflow.owner, workflow.project.id, workflow.run.run_id, published.item_id
                )
                self.assertNotEqual(retried.task_id, quality.task_id)
                _, next_execution = service.claim_test_engineer_task(
                    db, workflow.owner, workflow.project.id, workflow.run.run_id, retried.task_id
                )
                next_inputs = service.load_test_inputs(
                    db, workflow.project.id, workflow.run.run_id, code.item_id
                )
                self.assertEqual(next_inputs.inherited_suite, suite)
                self.assertIsNone(next_execution.draft)
                self.assertEqual(db.get(TaskExecution, execution.execution_id).status, "succeeded")
                run.status, run.active_slot = "failed", None
                db.commit()
                with self.assertRaises(ConflictException):
                    leader.retry_blocked_quality_validation(
                        db,
                        workflow.owner,
                        workflow.project.id,
                        workflow.run.run_id,
                        published.item_id,
                    )
                db.refresh(run)
                self.assertEqual(run.status, "failed")


@unittest.skipUnless(os.getenv("FORGEAI_TEST_DOCKER") == "1", "requires QA check image and Docker")
class RealQADockerTests(unittest.TestCase):
    def test_login_private_records_real_failure_repair_retest_and_full_regression(self):
        artifact_root = Path("output/qa-integration")
        artifact_root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="forgeai-qa-demo-") as directory:
            root = Path(directory) / "app"
            create_demo(root)
            spec = demo_spec()
            plan = build_acceptance_test_plan(
                SimpleNamespace(app_spec=spec, input_item=SimpleNamespace(item_id="ci_spec"))
            )
            state = QAState(
                app_spec=spec,
                code_item_id="ci_bad",
                code_source_hash=source_snapshot(root)[1],
                workspace_root=root,
                acceptance_test_plan=plan,
                acceptance_test_plan_item_id="ci_plan",
                acceptance_test_hash="c" * 64,
            )
            suite = demo_suite()
            result, _ = call(state, "design_test_cases", suite=suite.model_dump(mode="json"))
            self.assertTrue(result.ok, result.summary)
            result, _ = call(state, "run_acceptance_tests")
            (artifact_root / "failure.json").write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
            self.assertEqual(result.error_code, "ACCEPTANCE_NOT_PASSED", result.model_dump())
            rows = {row["test_id"]: row for row in state.executions[-1]["results"]}
            self.assertEqual(rows["test_ac_private"]["status"], "failed", rows)
            self.assertEqual(rows["test_ac_track"]["status"], "passed", rows)
            for key in ("credentials", "anonymous", "invalid"):
                self.assertEqual(rows[f"test_ac_{key}"]["status"], "passed", rows)
            result, _ = call(
                state,
                "record_defect",
                defect={
                    "defect_id": "private_records_leak",
                    "severity": "critical",
                    "title": "另一账号可以读取私有记录",
                    "description": "alice 保存后 bob 列表包含该记录",
                    "evidence": rows["test_ac_private"]["evidence"],
                    "related_requirement_ids": ["ac_private"],
                    "related_test_ids": ["test_ac_private"],
                    "execution_id": state.executions[-1]["execution_id"],
                },
            )
            self.assertTrue(result.ok, result.summary)
            for name, arguments in (
                ("run_check", {"check_id": "all"}),
                ("capture_screenshots", {}),
            ):
                result, _ = call(state, name, **arguments)
                self.assertTrue(result.ok, result.model_dump())
            result, failed_report = call(state, "write_test_report", test_report=report_for(state))
            self.assertTrue(result.ok, result.summary)
            self.assertEqual(failed_report.quality_conclusion.value, "failed")
            (artifact_root / "failed-report.json").write_text(
                failed_report.model_dump_json(indent=2), encoding="utf-8"
            )
            source = root / "backend/app/main.py"
            source.write_text(
                source.read_text(encoding="utf-8").replace(
                    "BUG_OWNER_FILTER = True", "BUG_OWNER_FILTER = False"
                ),
                encoding="utf-8",
            )
            repaired = QAState(
                app_spec=spec,
                code_item_id="ci_repaired",
                code_source_hash=source_snapshot(root)[1],
                workspace_root=root,
                acceptance_test_plan=plan,
                acceptance_test_plan_item_id="ci_plan",
                acceptance_test_hash="c" * 64,
                suite_versions=[failed_report.test_suite],
            )
            restore_test_engineer_progress(repaired, export_test_engineer_progress(state))
            self.assertEqual(repaired.executions, [])
            result, _ = call(repaired, "run_acceptance_tests")
            (artifact_root / "retest.json").write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
            self.assertTrue(result.ok, result.model_dump())
            self.assertEqual(result.data["suite_hash"], suite.digest())
            self.assertNotEqual(result.data["source_hash"], state.code_source_hash)
            for name, arguments in (
                ("run_check", {"check_id": "all"}),
                ("capture_screenshots", {}),
            ):
                result, _ = call(repaired, name, **arguments)
                self.assertTrue(result.ok, result.model_dump())
            result, report = call(repaired, "write_test_report", test_report=report_for(repaired))
            self.assertTrue(result.ok, result.summary)
            self.assertEqual(report.quality_conclusion.value, "passed")
            (artifact_root / "accepted-report.json").write_text(
                report.model_dump_json(indent=2), encoding="utf-8"
            )
