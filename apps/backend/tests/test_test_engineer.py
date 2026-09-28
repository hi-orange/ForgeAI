import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

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
from app.tools.test_engineer import (
    execute_test_engineer_tool,
    export_test_engineer_progress,
    restore_test_engineer_progress,
)

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
            },
            {
                "check_id": "visual",
                "status": "passed",
                "evidence": (
                    "截图证据：forgeai/evidence/hash/desktop.png、"
                    "forgeai/evidence/hash/mobile.png。"
                ),
            },
        ],
        "defects": [],
        "quality_conclusion": "passed",
        "summary": "已验证当前准确代码结果，核心验收和完整检查均通过。",
        "residual_risks": [],
    }


def visual_result(tool_call_id: str = "visual") -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_call_id=tool_call_id,
        name="capture_screenshots",
        ok=True,
        summary="桌面端与移动端截图通过",
        data={
            "check_id": "visual",
            "source_hash": SOURCE_HASH,
            "route": "/",
            "screenshots": [
                {"path": "forgeai/evidence/hash/desktop.png"},
                {"path": "forgeai/evidence/hash/mobile.png"},
            ],
        },
    )


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
                    "capture_screenshots",
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
            with patch(
                "app.tools.test_engineer.check_tools.capture_screenshots",
                return_value=visual_result(),
            ):
                captured, _ = execute_test_engineer_tool(
                    ToolCall(id="visual", name="capture_screenshots", arguments={"route": "/"}),
                    state,
                )
            self.assertTrue(captured.ok)
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
            state.checks["visual"] = visual_result()
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
        heartbeat = MagicMock()
        saved_progress = []
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
                id="visual",
                name="capture_screenshots",
                arguments={"route": "/"},
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
                patch(
                    "app.tools.test_engineer.check_tools.capture_screenshots",
                    return_value=visual_result(),
                ),
            ):
                report = test_engineer_agent.verify_code(
                    spec=valid_spec(),
                    system_design=SystemDesign.model_validate(valid_design()),
                    code_item_id="ci_code",
                    code_source_hash=SOURCE_HASH,
                    workspace_root=Path(directory),
                    heartbeat=heartbeat,
                    on_progress=saved_progress.append,
                )
        self.assertEqual(report.quality_conclusion, "passed")
        self.assertEqual(chat.call_count, 4)
        self.assertEqual(heartbeat.call_count, 8)
        self.assertEqual(len(saved_progress), 4)
        self.assertEqual(set(saved_progress[-1]["checks"]), {"all", "visual"})
        self.assertEqual(
            {tool.name for tool in chat.call_args_list[0].kwargs["tools"]},
            test_engineer_agent.TEST_ENGINEER_PROFILE.allowed_tools,
        )
        self.assertEqual(
            {tool.name for tool in chat.call_args_list[2].kwargs["tools"]},
            {"capture_screenshots"},
        )
        self.assertEqual(
            {tool.name for tool in chat.call_args_list[3].kwargs["tools"]},
            {"write_test_report"},
        )

    def test_agent_stops_immediately_when_visual_runtime_is_stale(self):
        actions = [
            ToolCall(id="check", name="run_check", arguments={"check_id": "all"}),
            ToolCall(id="visual", name="capture_screenshots", arguments={"route": "/"}),
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
                patch(
                    "app.tools.test_engineer.check_tools.capture_screenshots",
                    return_value=ToolExecutionResult(
                        tool_call_id="visual",
                        name="capture_screenshots",
                        ok=False,
                        error_code="CHECK_ENVIRONMENT_UNAVAILABLE",
                        summary="隔离截图镜像版本过旧，请重新构建后继续",
                        data={
                            "check_id": "visual",
                            "source_hash": SOURCE_HASH,
                            "route": "/",
                        },
                    ),
                ),
                self.assertRaisesRegex(BusinessException, "版本过旧"),
            ):
                test_engineer_agent.verify_code(
                    spec=valid_spec(),
                    system_design=SystemDesign.model_validate(valid_design()),
                    code_item_id="ci_code",
                    code_source_hash=SOURCE_HASH,
                    workspace_root=Path(directory),
                )
        self.assertEqual(chat.call_count, 2)

    def test_recovery_reuses_checks_for_the_same_source(self):
        with TemporaryDirectory(prefix="forgeai-test-engineer-resume-") as directory:
            root = Path(directory)
            previous = EngineerToolState(
                app_spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash=SOURCE_HASH,
                workspace_root=root,
            )
            previous.checks["all"] = ToolExecutionResult(
                tool_call_id="old_check",
                name="run_check",
                ok=True,
                summary="all 检查通过",
                data={"check_id": "all", "source_hash": SOURCE_HASH},
            )
            previous.checks["visual"] = visual_result("old_visual")
            progress = export_test_engineer_progress(previous)
            resumed = EngineerToolState(
                app_spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash=SOURCE_HASH,
                workspace_root=root,
            )
            restore_test_engineer_progress(resumed, progress)

            with (
                patch("app.tools.test_engineer.check_tools.run_check") as run,
                patch("app.tools.test_engineer.check_tools.capture_screenshots") as capture,
            ):
                checked, _ = execute_test_engineer_tool(
                    ToolCall(id="new_check", name="run_check", arguments={"check_id": "all"}),
                    resumed,
                )
                captured, _ = execute_test_engineer_tool(
                    ToolCall(
                        id="new_visual",
                        name="capture_screenshots",
                        arguments={"route": "/"},
                    ),
                    resumed,
                )

        self.assertIn("复用", checked.summary)
        self.assertIn("复用", captured.summary)
        run.assert_not_called()
        capture.assert_not_called()

    def test_recovery_rejects_other_source_and_transient_check_results(self):
        with TemporaryDirectory(prefix="forgeai-test-engineer-stale-") as directory:
            root = Path(directory)
            previous = EngineerToolState(
                app_spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash=SOURCE_HASH,
                workspace_root=root,
            )
            previous.checks["all"] = ToolExecutionResult(
                tool_call_id="timed_out",
                name="run_check",
                ok=False,
                error_code="CHECK_TIMEOUT",
                summary="检查超时",
                data={"check_id": "all", "source_hash": SOURCE_HASH},
            )
            progress = export_test_engineer_progress(previous)
            resumed = EngineerToolState(
                app_spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash=SOURCE_HASH,
                workspace_root=root,
            )
            restore_test_engineer_progress(resumed, progress)
            self.assertEqual(resumed.checks, {})

            progress["source_hash"] = "b" * 64
            progress["checks"]["all"]["error_code"] = None
            progress["checks"]["all"]["ok"] = True
            restore_test_engineer_progress(resumed, progress)
            self.assertEqual(resumed.checks, {})

    def test_recovery_discards_visual_result_from_old_runtime(self):
        with TemporaryDirectory(prefix="forgeai-test-engineer-stale-visual-") as directory:
            root = Path(directory)
            previous = EngineerToolState(
                app_spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash=SOURCE_HASH,
                workspace_root=root,
            )
            previous.checks["visual"] = ToolExecutionResult(
                tool_call_id="visual",
                name="capture_screenshots",
                ok=False,
                error_code="VISUAL_CHECK_FAILED",
                summary="浏览器截图失败",
                data={
                    "check_id": "visual",
                    "source_hash": SOURCE_HASH,
                    "route": "/",
                    "output": 'raise ValueError("Unknown check")',
                },
            )
            resumed = EngineerToolState(
                app_spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash=SOURCE_HASH,
                workspace_root=root,
            )
            restore_test_engineer_progress(resumed, export_test_engineer_progress(previous))
        self.assertNotIn("visual", resumed.checks)

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

    def test_explore_before_check_is_capped(self):
        with TemporaryDirectory(prefix="forgeai-test-engineer-cap-") as directory:
            root = Path(directory)
            (root / "frontend").mkdir()
            (root / "frontend" / "a.vue").write_text("<template></template>\n", encoding="utf-8")
            state = EngineerToolState(
                app_spec=valid_spec(),
                code_item_id="ci_code",
                code_source_hash=SOURCE_HASH,
                workspace_root=root,
            )
            for index in range(3):
                result, _ = execute_test_engineer_tool(
                    ToolCall(
                        id=f"read_{index}",
                        name="read_file",
                        arguments={"path": "frontend/a.vue"},
                    ),
                    state,
                )
                self.assertTrue(result.ok, result.summary)
            blocked, _ = execute_test_engineer_tool(
                ToolCall(id="read_blocked", name="read_file", arguments={"path": "frontend/a.vue"}),
                state,
            )
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.error_code, "CHECK_FIRST")
        self.assertIn("run_check", blocked.summary)


if __name__ == "__main__":
    unittest.main()
