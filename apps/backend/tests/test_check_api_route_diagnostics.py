"""Actionable API smoke failures from real runner output, without Docker or model calls."""

import unittest

from app.generation.engineering.checkpoint import new_checkpoint, record_check_failure
from app.generation.engineering.diagnostics import feedback
from app.schemas.agent_action import ToolExecutionResult
from app.tools.check_diagnostics import analyze_check_failure, extract_check_diagnostics


def failed_api_check(output: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_call_id="check",
        name="run_check",
        ok=False,
        error_code="CHECK_FAILED",
        summary="all 检查失败",
        arguments={"check_id": "all"},
        data={"check_id": "all", "source_hash": "source-1", "output": output},
    )


class ApiRouteDiagnosticTests(unittest.TestCase):
    def test_legacy_runner_output_identifies_missing_successful_route(self) -> None:
        output = (
            'INFO:     127.0.0.1:35228 - "GET /api/v1/jobs?q=designer HTTP/1.1" 404 Not Found\n'
            "RuntimeError: API 联调失败 public-list：期望 HTTP 200，实际 404；"
            '{"detail":"Not Found"}'
        )
        result = failed_api_check(output)
        issue = feedback(result).data["diagnostic"]["issues"][0]
        self.assertEqual(issue["code"], "public-list")
        self.assertEqual(issue["method"], "GET")
        self.assertEqual(issue["route"], "/api/v1/jobs?q=designer")
        self.assertEqual((issue["expected"], issue["actual"]), ("200", "404"))
        self.assertIn("真实接口", issue["suggested_action"])
        self.assertIn("保留等价强度的探针", issue["suggested_action"])
        checkpoint = new_checkpoint()
        record_check_failure(checkpoint, result, check_id="all")
        self.assertEqual(
            checkpoint["repair_context"]["diagnostics"][0]["route"],
            "/api/v1/jobs?q=designer",
        )

    def test_absent_request_log_still_guides_from_smoke_check_identity(self) -> None:
        output = "RuntimeError: API 联调失败 public-list：期望 HTTP 200，实际 404；Not Found"
        issue = extract_check_diagnostics(output)[0]
        self.assertIsNone(issue.method)
        self.assertIsNone(issue.route)
        self.assertIn("此探针 id", issue.suggested_action)

    def test_unrelated_access_log_does_not_misattribute_route(self) -> None:
        output = (
            'INFO:     127.0.0.1:35228 - "GET /api/v1/health HTTP/1.1" 200 OK\n'
            "RuntimeError: API 联调失败 public-list：期望 HTTP 200，实际 404；Not Found"
        )
        issue = extract_check_diagnostics(output)[0]
        self.assertIsNone(issue.route)

    def test_expected_not_found_does_not_receive_missing_route_guidance(self) -> None:
        output = "RuntimeError: API 联调失败 absent-job：期望 HTTP 404，实际 200；unexpected"
        issue = extract_check_diagnostics(output)[0]
        self.assertIsNone(issue.suggested_action)
        self.assertEqual(issue.expected, "404")
        self.assertEqual(issue.actual, "200")

    def test_route_changes_are_distinct_failure_evidence(self) -> None:
        a = failed_api_check(
            'INFO:     127.0.0.1:1 - "GET /api/v1/jobs HTTP/1.1" 404 Not Found\n'
            "RuntimeError: API 联调失败 public-list：期望 HTTP 200，实际 404；Not Found"
        )
        b = failed_api_check(
            'INFO:     127.0.0.1:2 - "GET /api/v2/jobs HTTP/1.1" 404 Not Found\n'
            "RuntimeError: API 联调失败 public-list：期望 HTTP 200，实际 404；Not Found"
        )
        self.assertNotEqual(
            analyze_check_failure(a).fingerprint, analyze_check_failure(b).fingerprint
        )


if __name__ == "__main__":
    unittest.main()
