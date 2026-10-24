"""Only stable, bounded QA facts should enter resumed model context."""

import json
import unittest

from qa_fixtures import unit_manifest, unit_suite

from app.agents.qa_failure_summary import MAX_SUMMARY_BYTES, summarize_qa_failures
from app.schemas.agent_action import ToolExecutionResult
from app.schemas.qa_suite import QASuite


def one_case_suite(steps: list[dict]) -> QASuite:
    raw = unit_suite().model_dump(mode="json")
    raw["scripts"]["test_ac_track"]["steps"] = steps
    return QASuite.model_validate(raw)


def failure_manifest(suite: QASuite, *, evidence: str, observations: list[dict]) -> dict:
    manifest = unit_manifest(suite, "failed")
    manifest["results"][0].update(evidence=evidence, observations=observations)
    return manifest


class QAFailureSummaryTests(unittest.TestCase):
    def test_api_status_mismatch_exposes_only_method_and_http_codes(self):
        suite = one_case_suite(
            [
                {
                    "kind": "api",
                    "method": "GET",
                    "path": "/api/jobs?email=private@example.com&token=SECRET_TOKEN",
                    "expect_status": 200,
                    "assertions": [
                        {"path": "$[0].title", "operator": "equals", "value": "private@example.com"}
                    ],
                }
            ]
        )
        manifest = failure_manifest(
            suite,
            evidence=(
                "GET /api/jobs?email=private@example.com&token=SECRET_TOKEN: "
                "expected HTTP 200, actual 404"
            ),
            observations=[],
        )
        facts = summarize_qa_failures(manifest, suite)
        case = facts["cases"][0]
        self.assertEqual(case["method"], "GET")
        self.assertEqual(case["expected_http_status"], 200)
        self.assertEqual(case["actual_http_status"], 404)
        self.assertEqual(case["category"], "http_status_mismatch")
        self.assertNotIn("private@example.com", json.dumps(facts))
        self.assertNotIn("SECRET_TOKEN", json.dumps(facts))
        self.assertNotIn("/api/jobs", json.dumps(facts))

    def test_json_assertion_reports_frozen_path_and_operator_without_values(self):
        suite = one_case_suite(
            [
                {
                    "kind": "api",
                    "method": "POST",
                    "path": "/api/jobs",
                    "expect_status": 201,
                    "assertions": [
                        {"path": "$.result.title", "operator": "equals", "value": "Secret Title"}
                    ],
                }
            ]
        )
        manifest = failure_manifest(
            suite,
            evidence="$.result.title equals: expected='Secret Title', actual='private@example.com'",
            observations=[],
        )
        case = summarize_qa_failures(manifest, suite)["cases"][0]
        self.assertEqual(case["method"], "POST")
        self.assertEqual(case["json_assertion"], {"path": "$.result.title", "operator": "equals"})
        self.assertNotIn("Secret Title", json.dumps(case))
        self.assertNotIn("private@example.com", json.dumps(case))

    def test_sensitive_json_path_is_not_copied(self):
        suite = one_case_suite(
            [
                {
                    "kind": "api",
                    "path": "/api/records",
                    "expect_status": 200,
                    "assertions": [
                        {"path": "$.access_token", "operator": "exists"},
                    ],
                }
            ]
        )
        manifest = failure_manifest(
            suite, evidence="$.access_token exists: expected=None, actual='SECRET'", observations=[]
        )
        case = summarize_qa_failures(manifest, suite)["cases"][0]
        self.assertNotIn("json_assertion", case)
        self.assertNotIn("SECRET", json.dumps(case))

    def test_browser_step_uses_safe_selector_and_category_only(self):
        suite = one_case_suite(
            [
                {"kind": "browser", "action": "navigate", "path": "/jobs?token=SECRET_TOKEN"},
                {
                    "kind": "browser",
                    "action": "expect_visible",
                    "selector": "[data-testid='job-card']",
                },
            ]
        )
        manifest = failure_manifest(
            suite,
            evidence="strict mode violation private@example.com URL /jobs?token=SECRET_TOKEN",
            observations=[{"kind": "browser"}],
        )
        manifest["results"][0]["status"] = "blocked"
        manifest["results"][0]["failure_kind"] = "script"
        case = summarize_qa_failures(manifest, suite)["cases"][0]
        self.assertEqual(case["failed_step_index"], 1)
        self.assertEqual(case["action"], "expect_visible")
        self.assertEqual(case["selector"], "[data-testid='job-card']")
        self.assertEqual(case["category"], "non_unique_locator")
        self.assertNotIn("private@example.com", json.dumps(case))
        self.assertNotIn("SECRET_TOKEN", json.dumps(case))

    def test_browser_sensitive_selector_and_mismatched_suite_omit_step(self):
        suite = one_case_suite(
            [
                {
                    "kind": "browser",
                    "action": "expect_visible",
                    "selector": "input[name=password]",
                }
            ]
        )
        manifest = failure_manifest(
            suite,
            evidence="Timeout waiting for input[name=password] private@example.com",
            observations=[],
        )
        case = summarize_qa_failures(manifest, suite)["cases"][0]
        self.assertEqual(case["category"], "timeout")
        self.assertNotIn("selector", case)
        manifest["suite_hash"] = "f" * 64
        self.assertEqual(summarize_qa_failures(manifest, suite)["cases"], [])

    def test_all_and_visual_checks_keep_only_safe_diagnostic_fields(self):
        all_check = ToolExecutionResult(
            tool_call_id="all",
            name="run_check",
            ok=False,
            error_code="CHECK_FAILED",
            summary="private@example.com",
            data={
                "output": (
                    "src/pages/JobsPage.tsx(42,8): error TS2322: "
                    "token PRIVATE_TOKEN private@example.com\n"
                    'File "/tmp/app/backend/app/routes/jobs.py", line 17, in list_jobs\n'
                    "NameError: private@example.com SECRET_TOKEN"
                )
            },
        )
        visual_check = ToolExecutionResult(
            tool_call_id="visual",
            name="capture_screenshots",
            ok=False,
            error_code="VISUAL_FAILED",
            summary="private@example.com",
            data={
                "output": (
                    'File "C:/Users/private@example.com/secret.py", line 3\nNameError: token'
                )
            },
        )
        visual_key = "visual_0123456789ab"
        earlier_visual = visual_check.model_copy(update={"error_code": "OLDER_VISUAL_FAILED"})
        facts = summarize_qa_failures(
            None,
            None,
            {"all": all_check, "visual_aaaaaaaaaaaa": earlier_visual, visual_key: visual_check},
        )
        self.assertEqual(facts["checks"]["all"]["error_code"], "CHECK_FAILED")
        self.assertEqual(
            facts["checks"]["all"]["diagnostics"][0],
            {"kind": "typescript", "code": "TS2322", "path": "src/pages/JobsPage.tsx", "line": 42},
        )
        self.assertEqual(facts["checks"][visual_key]["error_code"], "VISUAL_FAILED")
        self.assertNotIn("visual_aaaaaaaaaaaa", facts["checks"])
        serialized = json.dumps(facts)
        self.assertNotIn("private@example.com", serialized)
        self.assertNotIn("PRIVATE_TOKEN", serialized)
        self.assertNotIn("C:/Users", serialized)

    def test_case_count_and_total_serialized_bytes_are_bounded(self):
        raw = unit_suite().model_dump(mode="json")
        scenario = raw["scenarios"][0]
        script = raw["scripts"]["test_ac_track"]
        raw["scenarios"] = [{**scenario, "test_id": f"test_ac_{index:02d}"} for index in range(40)]
        raw["scripts"] = {f"test_ac_{index:02d}": script for index in range(40)}
        suite = QASuite.model_validate(raw)
        manifest = unit_manifest(suite, "failed")
        for row in manifest["results"]:
            row["evidence"] = "GET /api/x?token=SECRET_TOKEN: expected HTTP 200, actual 404"
            row["observations"] = []
        facts = summarize_qa_failures(manifest, suite)
        self.assertEqual(facts["nonpassed_total"], 40)
        self.assertLessEqual(len(facts["cases"]), 16)
        self.assertEqual(facts["omitted_case_count"], 40 - len(facts["cases"]))
        serialized = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(serialized.encode("utf-8")), MAX_SUMMARY_BYTES)
        self.assertNotIn("SECRET_TOKEN", serialized)


if __name__ == "__main__":
    unittest.main()
