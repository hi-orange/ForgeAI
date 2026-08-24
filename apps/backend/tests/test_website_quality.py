import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.website_quality import (
    ValidationIssue,
    WebsiteValidationReport,
    deterministic_issues,
)
from app.services.agent import start_website_build


def website_files(html: str, script: str = "") -> str:
    return json.dumps(
        {
            "index.html": html,
            "style.css": "body { color: #111; min-height: 100vh; }",
            "script.js": script,
        },
        ensure_ascii=False,
    )


class DeterministicWebsiteQualityTests(unittest.TestCase):
    def test_detects_broken_references_and_disallowed_network_access(self) -> None:
        files = website_files(
            "<!doctype html><html><body>"
            '<main id="content"><div id="content"></div>'
            '<img src="https://example.com/image.png"></main>'
            "</body></html>",
            "document.getElementById('missing'); fetch('/api/jobs');",
        )

        issues = deterministic_issues(files)
        descriptions = "\n".join(issue.description for issue in issues)

        self.assertIn("重复 DOM id", descriptions)
        self.assertIn("不存在的 DOM id", descriptions)
        self.assertIn("远程资源", descriptions)
        self.assertIn("网络请求", descriptions)

    def test_accepts_ids_created_dynamically_by_script(self) -> None:
        files = website_files(
            (
                '<!doctype html><html><head><meta charset="utf-8"><title>Test</title></head>'
                '<body><main id="app"><h1>Dynamic component test</h1></main></body></html>'
            ),
            (
                "const button = document.createElement('button');"
                "button.id = 'apply-btn';"
                "document.getElementById('apply-btn');"
            ),
        )

        issues = deterministic_issues(files)

        self.assertFalse(any("apply-btn" in issue.description for issue in issues))

    def test_rejects_form_without_prevented_submit(self) -> None:
        files = website_files(
            (
                '<!doctype html><html><head><meta charset="utf-8"><title>Form</title></head>'
                '<body><form id="contact"><input required><button>提交</button></form>'
                "</body></html>"
            ),
            "document.getElementById('contact').addEventListener('change', () => {});",
        )

        issues = deterministic_issues(files)

        self.assertTrue(any("默认提交" in issue.description for issue in issues))

    def test_accepts_locally_handled_form_submit(self) -> None:
        files = website_files(
            (
                '<!doctype html><html><head><meta charset="utf-8"><title>Form</title></head>'
                '<body><form id="contact"><input required><button>提交</button></form>'
                "</body></html>"
            ),
            (
                "document.getElementById('contact').addEventListener('submit', (event) => {"
                "event.preventDefault(); });"
            ),
        )

        issues = deterministic_issues(files)

        self.assertFalse(any("默认提交" in issue.description for issue in issues))


class WebsiteRepairLoopTests(unittest.TestCase):
    def test_repairs_failed_draft_then_accepts_revalidated_result(self) -> None:
        draft = website_files(
            '<!doctype html><html><head><meta charset="utf-8"><title>Draft</title></head>'
            "<body><main><h1>draft website content</h1></main></body></html>"
        )
        repaired = website_files(
            '<!doctype html><html><head><meta charset="utf-8"><title>Fixed</title></head>'
            "<body><main><h1>fixed website content</h1></main></body></html>"
        )
        issue = ValidationIssue(
            severity="error",
            category="interaction",
            file="index.html",
            description="弹窗缺少退出路径",
            suggestion="补充完整且可访问的关闭交互",
        )

        class FakeBuilder:
            def __init__(self) -> None:
                self.repair_calls = 0

            def run(self, approved_spec: str) -> str:
                return draft

            def repair(self, approved_spec: str, current_files: str, issues: str) -> str:
                self.repair_calls += 1
                return repaired

        class FakeQualityAgent:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, approved_spec: str, generated_files: str) -> WebsiteValidationReport:
                self.calls += 1
                if self.calls == 1:
                    return WebsiteValidationReport(passed=False, issues=[issue])
                return WebsiteValidationReport(passed=True, issues=[])

        builder = FakeBuilder()
        quality_agent = FakeQualityAgent()
        project = SimpleNamespace(id=16, approved_spec='{"version":"1.0"}')

        with (
            patch("app.services.agent.WebsiteBuilderAgent", return_value=builder),
            patch("app.services.agent.WebsiteQualityAgent", return_value=quality_agent),
            patch("app.services.agent.deterministic_issues", return_value=[]),
        ):
            _, generated_files, report_json = start_website_build(project)

        report = json.loads(report_json)
        generated = json.loads(generated_files)
        self.assertIn("fixed website content", generated["index.html"])
        self.assertIn("data-forge-id", generated["index.html"])
        self.assertEqual(builder.repair_calls, 1)
        self.assertEqual(quality_agent.calls, 2)
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["attempts"]), 2)


if __name__ == "__main__":
    unittest.main()
