import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from app.agents import architect as architect_agent
from app.agents.roles import get_role_profile
from app.core.exceptions import BusinessException
from app.models.task import TaskRecipient
from app.schemas.agent_action import ChatWithToolsResult, ToolCall, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.services import architect as architect_service
from app.tools.architect import ArchitectToolState, execute_architect_tool


def valid_spec() -> AppSpec:
    return AppSpec.model_validate(
        {
            "goal": "记录个人阅读情况",
            "target_users": ["个人读者"],
            "features": [{"id": "feat_track", "text": "记录书籍和阅读状态"}],
            "data_requirements": [{"id": "data_book", "text": "书籍与阅读状态"}],
            "interface_requirements": [{"id": "ui_list", "text": "阅读列表"}],
            "constraints": [{"id": "con_private", "text": "只允许本人查看"}],
            "acceptance_criteria": [
                {
                    "id": "ac_track",
                    "text": "用户保存后可以在列表中看到记录",
                    "source_ids": ["feat_track"],
                }
            ],
            "open_questions": [],
        }
    )


def valid_design() -> dict:
    return {
        "architecture_overview": "React 调用 FastAPI，FastAPI 通过 SQLAlchemy 持久化到 SQLite。",
        "architecture_notes": ["采用单体分层结构，保持部署和调试简单。"],
        "modules": [
            {
                "name": "Reading records",
                "responsibility": "管理当前用户自己的阅读记录。",
                "dependencies": ["SQLite"],
            }
        ],
        "interfaces": [
            {
                "name": "Create reading record",
                "kind": "http",
                "purpose": "保存一条阅读记录。",
                "method": "POST",
                "path": "/api/reading-records",
                "inputs": ["title", "status"],
                "outputs": ["record"],
            }
        ],
        "data_structures": [
            {
                "name": "ReadingRecord",
                "purpose": "保存用户的书籍阅读状态。",
                "fields": ["id", "owner_id", "title", "status"],
                "relationships": ["owner_id belongs to current user"],
            }
        ],
        "technology_choices": [
            {
                "area": "Backend",
                "choice": "FastAPI + SQLAlchemy",
                "rationale": "沿用固定技术栈并提供显式 API 契约。",
            }
        ],
        "constraints": ["所有查询按当前用户过滤。"],
        "implementation_dependencies": [],
    }


class ArchitectTests(unittest.TestCase):
    def test_partial_draft_survives_interruption_and_submits_without_repeating_design(self):
        progress = []
        design = valid_design()
        keys = list(design)
        first = {key: design[key] for key in keys[:3]}
        second = {key: design[key] for key in keys[3:]}
        with TemporaryDirectory() as directory:
            with patch.object(
                architect_agent,
                "chat_with_tools",
                side_effect=[
                    ChatWithToolsResult(tool_calls=[ToolCall(id="r", name="read_artifact")]),
                    ChatWithToolsResult(
                        tool_calls=[
                            ToolCall(id="p", name="editor_patch", arguments={"sections": first})
                        ]
                    ),
                    BusinessException("interrupted"),
                ],
            ):
                with self.assertRaisesRegex(BusinessException, "interrupted"):
                    architect_agent.generate_system_design(
                        spec=valid_spec(),
                        workspace_root=Path(directory),
                        on_checkpoint=progress.append,
                    )
            saved = progress[-1]
            self.assertEqual(saved["state"]["draft_sections"], first)
            self.assertEqual(saved["turns"], 3)
            with patch.object(
                architect_agent,
                "chat_with_tools",
                side_effect=[
                    ChatWithToolsResult(
                        tool_calls=[
                            ToolCall(id="p2", name="editor_patch", arguments={"sections": second})
                        ]
                    ),
                    ChatWithToolsResult(tool_calls=[ToolCall(id="s", name="write_system_design")]),
                ],
            ):
                result = architect_agent.generate_system_design(
                    spec=valid_spec(),
                    workspace_root=Path(directory),
                    checkpoint=saved,
                    on_checkpoint=progress.append,
                )
            self.assertEqual(result.model_dump(mode="json"), design)
            self.assertEqual(progress[-1]["turns"], 5)

    def test_patch_is_atomic_and_submission_still_checks_full_frozen_contract(self):
        with TemporaryDirectory() as directory:
            state = ArchitectToolState(app_spec=valid_spec(), workspace_root=Path(directory))

            def call(name, **arguments):
                return execute_architect_tool(
                    ToolCall(id="x", name=name, arguments=arguments), state
                )

            call("editor_patch", sections={"architecture_overview": "first"})
            result, _ = call(
                "editor_patch", sections={"architecture_overview": "changed", "modules": []}
            )
            self.assertFalse(result.ok)
            self.assertEqual(state.draft_sections, {"architecture_overview": "first"})
            self.assertFalse(call("write_system_design")[0].ok)
            call("read_artifact")
            self.assertFalse(call("write_system_design")[0].ok)
            call("editor_patch", sections=valid_design())
            result, _ = call(
                "editor_patch",
                sections={"architecture_notes": ["Additional risk"]},
                append_lists=True,
            )
            self.assertTrue(result.ok)
            self.assertEqual(len(state.draft_sections["architecture_notes"]), 2)
            call(
                "editor_patch",
                sections={
                    "implementation_dependencies": [
                        {
                            "prerequisite_requirement_id": "unknown",
                            "dependent_requirement_id": "feat_track",
                            "reason": "invalid reference",
                        }
                    ]
                },
            )
            self.assertFalse(call("write_system_design")[0].ok)
            call("editor_patch", sections={"implementation_dependencies": []})
            self.assertTrue(call("write_system_design")[0].ok)

    def test_recovers_design_draft_and_inspections_without_restarting_tool_loop(self):
        progress = []
        initial = [
            ChatWithToolsResult(tool_calls=[ToolCall(id="intent", name="read_artifact")]),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="draft", name="editor_write", arguments={"system_design": valid_design()}
                    )
                ]
            ),
            BusinessException("provider unavailable"),
        ]
        with TemporaryDirectory(prefix="forgeai-architect-resume-") as directory:
            with patch.object(architect_agent, "chat_with_tools", side_effect=initial):
                with self.assertRaisesRegex(BusinessException, "provider unavailable"):
                    architect_agent.generate_system_design(
                        spec=valid_spec(),
                        workspace_root=Path(directory),
                        on_checkpoint=progress.append,
                    )
            self.assertEqual(progress[-1]["turns"], 3)
            self.assertIn("artifact:app_spec", progress[-1]["state"]["inspection_keys"])
            with patch.object(
                architect_agent,
                "chat_with_tools",
                return_value=ChatWithToolsResult(
                    tool_calls=[
                        ToolCall(
                            id="finish",
                            name="write_system_design",
                            arguments={"system_design": valid_design()},
                        )
                    ]
                ),
            ) as chat:
                result = architect_agent.generate_system_design(
                    spec=valid_spec(),
                    workspace_root=Path(directory),
                    checkpoint=progress[-1],
                    on_checkpoint=progress.append,
                )
            self.assertEqual(result.model_dump(), valid_design())
            self.assertEqual(progress[-1]["turns"], 4)
            self.assertEqual(chat.call_count, 1)
            with patch.object(architect_agent, "chat_with_tools") as chat:
                replay = architect_agent.generate_system_design(
                    spec=valid_spec(), workspace_root=Path(directory), checkpoint=progress[-1]
                )
                self.assertEqual(replay, result)
                chat.assert_not_called()

    def test_invalid_model_response_uses_bounded_correction_without_losing_context(self):
        from app.core.exceptions import ModelProtocolException

        with TemporaryDirectory(prefix="forgeai-architect-protocol-") as directory:
            turns = [
                ChatWithToolsResult(tool_calls=[ToolCall(id="intent", name="read_artifact")]),
                ModelProtocolException("工具参数不是合法 JSON"),
                ChatWithToolsResult(
                    tool_calls=[
                        ToolCall(
                            id="finish",
                            name="write_system_design",
                            arguments={"system_design": valid_design()},
                        )
                    ]
                ),
            ]
            progress = []
            with patch.object(architect_agent, "chat_with_tools", side_effect=turns) as chat:
                result = architect_agent.generate_system_design(
                    spec=valid_spec(), workspace_root=Path(directory), on_checkpoint=progress.append
                )
            self.assertEqual(result.model_dump(), valid_design())
            self.assertEqual(progress[-1]["turns"], 3)
            self.assertIn("未执行任何工具", chat.call_args.kwargs["messages"][-1]["content"])

    def test_role_has_requested_goal_deliverable_and_tools(self):
        profile = get_role_profile(TaskRecipient.ARCHITECT)
        self.assertEqual(profile.display_name, "Architect")
        self.assertIn("简洁、可用、完整", profile.goal)
        self.assertEqual(profile.deliverables, ("system_design",))
        self.assertEqual(
            profile.allowed_tools,
            frozenset(
                {
                    "read_artifact",
                    "editor_read",
                    "editor_write",
                    "editor_patch",
                    "terminal_list",
                    "terminal_run",
                    "write_system_design",
                }
            ),
        )

    def test_system_design_requires_all_implementation_sections(self):
        design = SystemDesign.model_validate(valid_design())
        self.assertEqual(design.modules[0].name, "Reading records")
        for missing in (
            "architecture_notes",
            "modules",
            "interfaces",
            "data_structures",
            "technology_choices",
        ):
            payload = valid_design()
            payload.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValidationError):
                SystemDesign.model_validate(payload)

    def test_editor_and_terminal_tools_are_bounded(self):
        with TemporaryDirectory(prefix="forgeai-architect-") as directory:
            root = Path(directory)
            (root / "README.md").write_text("template notes\n", encoding="utf-8")
            state = ArchitectToolState(app_spec=valid_spec(), workspace_root=root)

            listed, _ = execute_architect_tool(
                ToolCall(id="list", name="terminal_list", arguments={"path": "."}), state
            )
            self.assertTrue(listed.ok)
            self.assertEqual(listed.name, "terminal_list")

            read, _ = execute_architect_tool(
                ToolCall(
                    id="read",
                    name="editor_read",
                    arguments={"target": "README.md"},
                ),
                state,
            )
            self.assertTrue(read.ok)
            self.assertEqual(read.data["content"], "template notes")

            saved, _ = execute_architect_tool(
                ToolCall(
                    id="edit",
                    name="editor_write",
                    arguments={"system_design": valid_design()},
                ),
                state,
            )
            self.assertTrue(saved.ok)
            self.assertIsNotNone(state.draft)

            rejected, _ = execute_architect_tool(
                ToolCall(
                    id="unsafe",
                    name="terminal_run",
                    arguments={"argv": ["python", "dangerous.py"]},
                ),
                state,
            )
            self.assertFalse(rejected.ok)
            self.assertEqual(rejected.error_code, "TOOL_REJECTED")

            with patch(
                "app.tools.architect._run_terminal",
                return_value={"argv": ["rg", "--files"], "exit_code": 0, "output": "README.md"},
            ) as run:
                terminal, _ = execute_architect_tool(
                    ToolCall(
                        id="terminal",
                        name="terminal_run",
                        arguments={"argv": ["rg", "--files"]},
                    ),
                    state,
                )
            self.assertTrue(terminal.ok)
            run.assert_called_once_with(root, ["rg", "--files"])

            repeated, _ = execute_architect_tool(
                ToolCall(
                    id="terminal-repeat",
                    name="terminal_run",
                    arguments={"argv": ["rg", "--files"]},
                ),
                state,
            )
            self.assertFalse(repeated.ok)
            self.assertEqual(repeated.error_code, "ARCHITECT_CONTEXT_REUSED")
            self.assertIn("已经读取过", repeated.summary)

    def test_context_budget_is_a_recoverable_internal_signal(self):
        with TemporaryDirectory(prefix="forgeai-architect-budget-") as directory:
            root = Path(directory)
            state = ArchitectToolState(app_spec=valid_spec(), workspace_root=root)
            state.inspection_count = 4

            observation, output = execute_architect_tool(
                ToolCall(id="extra", name="terminal_list", arguments={"path": "."}),
                state,
            )

        self.assertIsNone(output)
        self.assertFalse(observation.ok)
        self.assertEqual(observation.error_code, "ARCHITECT_CONTEXT_READY")
        self.assertNotIn("预算已用尽", observation.summary)

    def test_recoverable_context_signal_is_not_user_visible(self):
        observation = ToolExecutionResult(
            tool_call_id="extra",
            name="editor_read",
            ok=False,
            error_code="ARCHITECT_CONTEXT_READY",
            summary="Architect 上下文已充分，请提交系统设计",
        )
        db = MagicMock()
        with (
            patch.object(
                architect_service.task_execution,
                "require_execution",
                return_value=MagicMock(),
            ) as require_execution,
            patch.object(
                architect_service.task_execution, "renew_execution_lease"
            ) as renew_execution_lease,
        ):
            architect_service.record_architect_activity(db, "task", "execution", observation)
        require_execution.assert_called_once_with(db, "task", "execution", lock=True)
        renew_execution_lease.assert_called_once_with(db, "task", "execution")

    def test_agent_edits_then_submits_validated_system_design(self):
        turns = [
            ChatWithToolsResult(
                tool_calls=[ToolCall(id="intent", name="read_artifact", arguments={})]
            ),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="edit",
                        name="editor_write",
                        arguments={"system_design": valid_design()},
                    )
                ]
            ),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="submit",
                        name="write_system_design",
                        arguments={"system_design": valid_design()},
                    )
                ]
            ),
        ]
        with TemporaryDirectory(prefix="forgeai-architect-agent-") as directory:
            with patch.object(architect_agent, "chat_with_tools", side_effect=turns) as chat:
                result = architect_agent.generate_system_design(
                    spec=valid_spec(), workspace_root=Path(directory)
                )

        self.assertEqual(result.model_dump(), valid_design())
        self.assertEqual(chat.call_count, 3)
        self.assertEqual(
            {tool.name for tool in chat.call_args_list[0].kwargs["tools"]},
            architect_agent.ARCHITECT_PROFILE.allowed_tools,
        )
        self.assertEqual(
            [message["role"] for message in chat.call_args_list[1].kwargs["messages"]],
            ["system", "user", "assistant", "tool"],
        )

    def test_agent_executes_read_batch_without_failing_the_execution(self):
        turns = [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(id="artifact", name="read_artifact", arguments={}),
                    ToolCall(
                        id="extra-read",
                        name="editor_read",
                        arguments={"target": "README.md"},
                    ),
                ]
            ),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="submit",
                        name="write_system_design",
                        arguments={"system_design": valid_design()},
                    )
                ]
            ),
        ]
        observations = []
        with TemporaryDirectory(prefix="forgeai-architect-agent-") as directory:
            (Path(directory) / "README.md").write_text("source context", encoding="utf-8")
            with patch.object(architect_agent, "chat_with_tools", side_effect=turns) as chat:
                result = architect_agent.generate_system_design(
                    spec=valid_spec(),
                    workspace_root=Path(directory),
                    on_activity=observations.append,
                )

        self.assertEqual(result.model_dump(), valid_design())
        self.assertEqual(
            [item.error_code for item in observations],
            [None, None, None],
        )
        history = chat.call_args_list[1].kwargs["messages"]
        self.assertEqual(
            [message["role"] for message in history],
            ["system", "user", "assistant", "tool", "tool"],
        )
        self.assertEqual(history[-1]["tool_call_id"], "extra-read")

    def test_agent_rejects_unapproved_tools_and_missing_workspace(self):
        with self.assertRaisesRegex(BusinessException, "工作区不存在"):
            architect_agent.generate_system_design(
                spec=valid_spec(), workspace_root=Path("missing-architect-workspace")
            )
        with TemporaryDirectory(prefix="forgeai-architect-agent-") as directory:
            with patch.object(
                architect_agent,
                "chat_with_tools",
                return_value=ChatWithToolsResult(
                    tool_calls=[ToolCall(id="patch", name="apply_patch", arguments={})]
                ),
            ):
                with self.assertRaisesRegex(BusinessException, "无权使用工具"):
                    architect_agent.generate_system_design(
                        spec=valid_spec(), workspace_root=Path(directory)
                    )


if __name__ == "__main__":
    unittest.main()


class MissingArchitectPathTests(unittest.TestCase):
    def test_missing_file_is_feedback_and_next_path_can_succeed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "exists.txt").write_text("evidence", encoding="utf-8")
            state = ArchitectToolState(app_spec=valid_spec(), workspace_root=root)
            missing, output = execute_architect_tool(
                ToolCall(id="missing", name="editor_read", arguments={"target": "absent.txt"}),
                state,
            )
            self.assertFalse(missing.ok)
            self.assertEqual(missing.error_code, "TOOL_REJECTED")
            self.assertIsNone(output)
            present, _ = execute_architect_tool(
                ToolCall(id="present", name="editor_read", arguments={"target": "exists.txt"}),
                state,
            )
            self.assertTrue(present.ok)
