import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError
from test_architect import valid_design, valid_spec

from app.agents import test_engineer as test_engineer_agent
from app.agents.roles import get_role_profile
from app.core.exceptions import BusinessException
from app.models.task import TaskRecipient
from app.schemas.agent_action import ChatWithToolsResult, ToolCall, ToolExecutionResult
from app.schemas.system_design import SystemDesign
from app.schemas.test_report import TestReport as QualityReport
from app.tools.test_engineer import (
    TestEngineerToolState as EngineerToolState,
)
from app.tools.test_engineer import execute_test_engineer_tool

SOURCE_HASH = "a" * 64


def passed_report() -> dict:
    return {
        "code_item_id": "ci_code",
        "source_hash": SOURCE_HASH,
        "requirement_results": [
            {
                "requirement_id": "ac_track",
                "status": "passed",
                "evidence": "保存记录后通过接口和列表响应观察到该记录。",
            }
        ],
        "check_results": [
            {
                "check_id": "all",
                "status": "passed",
                "evidence": "数据库、后端和前端完整检查退出码为 0。",
            }
        ],
        "defects": [],
        "quality_conclusion": "passed",
        "summary": "已验证当前准确代码结果，核心验收和完整检查均通过。",
        "residual_risks": [],
    }


def failed_report() -> dict:
    report = passed_report()
    report["requirement_results"][0]["status"] = "failed"
    report["requirement_results"][0]["evidence"] = "保存接口返回 500。"
    report["check_results"][0]["status"] = "failed"
    report["check_results"][0]["evidence"] = "后端启动检查失败。"
    report["defects"] = [
        {
            "defect_id": "defect_save_500",
            "severity": "critical",
            "title": "保存阅读记录返回 500",
            "description": "调用保存接口时发生未处理异常。",
            "evidence": "完整检查输出显示 POST /api/reading-records 返回 500。",
            "related_requirement_ids": ["ac_track"],
        }
    ]
    report["quality_conclusion"] = "failed"
    report["summary"] = "核心保存流程失败，当前代码不能通过质量验证。"
    return report


class TestEngineerTests(unittest.TestCase):
    def test_role_is_independent_and_has_no_code_write_tool(self):
        profile = get_role_profile(TaskRecipient.TEST_ENGINEER)
        self.assertIn("独立验证", profile.goal)
        self.assertIn("PRD 和系统设计", profile.goal)
        self.assertEqual(profile.deliverables, ("test_report",))
        self.assertEqual(
            profile.allowed_tools,
            frozenset(
                {
                    "read_artifact",
                    "list_files",
                    "read_file",
                    "search_code",
                    "run_check",
                    "record_defect",
                    "write_test_report",
                }
            ),
        )
        self.assertNotIn("apply_patch", profile.allowed_tools)

    def test_report_cannot_claim_pass_with_defects_or_failed_evidence(self):
        self.assertEqual(QualityReport.model_validate(passed_report()).quality_conclusion, "passed")
        invalid = passed_report()
        invalid["defects"] = failed_report()["defects"]
        with self.assertRaises(ValidationError):
            QualityReport.model_validate(invalid)

    def test_tools_run_real_check_and_submit_exact_code_report(self):
        with TemporaryDirectory(prefix="forgeai-test-engineer-") as directory:
            state = EngineerToolState(
                app_spec=valid_spec(),
                system_design=SystemDesign.model_validate(valid_design()),
                code_item_id="ci_code",
                code_source_hash=SOURCE_HASH,
                workspace_root=Path(directory),
            )
            with patch(
                "app.tools.test_engineer.check_tools.run_check",
                return_value=ToolExecutionResult(
                    tool_call_id="check",
                    name="run_check",
                    ok=True,
                    summary="all 检查通过",
                    data={"check_id": "all", "source_hash": SOURCE_HASH},
                ),
            ) as run:
                checked, _ = execute_test_engineer_tool(
                    ToolCall(
                        id="check",
                        name="run_check",
                        arguments={"check_id": "all"},
                    ),
                    state,
                )
            self.assertTrue(checked.ok)
            run.assert_called_once_with(Path(directory), check_id="all", tool_call_id="check")
            submitted, report = execute_test_engineer_tool(
                ToolCall(
                    id="report",
                    name="write_test_report",
                    arguments={"test_report": passed_report()},
                ),
                state,
            )
        self.assertTrue(submitted.ok)
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.code_item_id, "ci_code")

    def test_failed_acceptance_requires_recorded_defect(self):
        with TemporaryDirectory(prefix="forgeai-test-engineer-") as directory:
            state = EngineerToolState(
                app_spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash=SOURCE_HASH,
                workspace_root=Path(directory),
            )
            with patch(
                "app.tools.test_engineer.check_tools.run_check",
                return_value=ToolExecutionResult(
                    tool_call_id="check",
                    name="run_check",
                    ok=False,
                    error_code="CHECK_FAILED",
                    summary="all 检查失败",
                    data={"check_id": "all", "source_hash": SOURCE_HASH},
                ),
            ):
                execute_test_engineer_tool(
                    ToolCall(
                        id="check",
                        name="run_check",
                        arguments={"check_id": "all"},
                    ),
                    state,
                )
            missing_defect = failed_report()
            missing_defect["defects"] = []
            rejected, no_report = execute_test_engineer_tool(
                ToolCall(
                    id="premature",
                    name="write_test_report",
                    arguments={"test_report": missing_defect},
                ),
                state,
            )
            self.assertFalse(rejected.ok)
            self.assertIsNone(no_report)

            recorded, _ = execute_test_engineer_tool(
                ToolCall(
                    id="defect",
                    name="record_defect",
                    arguments={"defect": failed_report()["defects"][0]},
                ),
                state,
            )
            self.assertTrue(recorded.ok)
            submitted, report = execute_test_engineer_tool(
                ToolCall(
                    id="failed_report",
                    name="write_test_report",
                    arguments={"test_report": failed_report()},
                ),
                state,
            )
            self.assertTrue(submitted.ok)
            self.assertEqual(report.quality_conclusion, "failed")

    def test_source_mismatch_blocks_check_evidence(self):
        with TemporaryDirectory(prefix="forgeai-test-engineer-") as directory:
            state = EngineerToolState(
                app_spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash=SOURCE_HASH,
                workspace_root=Path(directory),
            )
            with patch(
                "app.tools.test_engineer.check_tools.run_check",
                return_value=ToolExecutionResult(
                    tool_call_id="check",
                    name="run_check",
                    ok=True,
                    summary="all 检查通过",
                    data={"check_id": "all", "source_hash": "b" * 64},
                ),
            ):
                result, _ = execute_test_engineer_tool(
                    ToolCall(
                        id="check",
                        name="run_check",
                        arguments={"check_id": "all"},
                    ),
                    state,
                )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "SOURCE_MISMATCH")

    def test_agent_runs_check_then_submits_report(self):
        actions = [
            ToolCall(
                id="code",
                name="read_artifact",
                arguments={"artifact": "code"},
            ),
            ToolCall(
                id="check",
                name="run_check",
                arguments={"check_id": "all"},
            ),
            ToolCall(
                id="report",
                name="write_test_report",
                arguments={"test_report": passed_report()},
            ),
        ]
        with TemporaryDirectory(prefix="forgeai-test-engineer-agent-") as directory:
            with (
                patch.object(
                    test_engineer_agent,
                    "chat_with_tools",
                    side_effect=[ChatWithToolsResult(tool_calls=[call]) for call in actions],
                ) as chat,
                patch(
                    "app.tools.test_engineer.check_tools.run_check",
                    return_value=ToolExecutionResult(
                        tool_call_id="check",
                        name="run_check",
                        ok=True,
                        summary="all 检查通过",
                        data={"check_id": "all", "source_hash": SOURCE_HASH},
                    ),
                ),
            ):
                report = test_engineer_agent.verify_code(
                    spec=valid_spec(),
                    system_design=SystemDesign.model_validate(valid_design()),
                    code_item_id="ci_code",
                    code_source_hash=SOURCE_HASH,
                    workspace_root=Path(directory),
                )
        self.assertEqual(report.quality_conclusion, "passed")
        self.assertEqual(chat.call_count, 3)
        self.assertEqual(
            {tool.name for tool in chat.call_args_list[0].kwargs["tools"]},
            test_engineer_agent.TEST_ENGINEER_PROFILE.allowed_tools,
        )

    def test_agent_rejects_code_write_tool(self):
        with TemporaryDirectory(prefix="forgeai-test-engineer-agent-") as directory:
            with patch.object(
                test_engineer_agent,
                "chat_with_tools",
                return_value=ChatWithToolsResult(
                    tool_calls=[ToolCall(id="write", name="apply_patch", arguments={})]
                ),
            ):
                with self.assertRaisesRegex(BusinessException, "无权使用工具"):
                    test_engineer_agent.verify_code(
                        spec=valid_spec(),
                        system_design=SystemDesign.model_validate(valid_design()),
                        code_item_id="ci_code",
                        code_source_hash=SOURCE_HASH,
                        workspace_root=Path(directory),
                    )


if __name__ == "__main__":
    unittest.main()
