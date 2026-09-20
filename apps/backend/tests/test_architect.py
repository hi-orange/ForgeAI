import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from app.agents import architect as architect_agent
from app.agents.roles import get_role_profile
from app.core.exceptions import BusinessException
from app.models.task import TaskRecipient
from app.schemas.agent_action import ChatWithToolsResult, ToolCall
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
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
        "architecture_overview": "Vue 调用 FastAPI，FastAPI 通过 SQLAlchemy 持久化到 SQLite。",
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
    }


class ArchitectTests(unittest.TestCase):
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
            self.assertIn("已经读取过", repeated.summary)

    def test_agent_edits_then_submits_validated_system_design(self):
        turns = [
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
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(
            {tool.name for tool in chat.call_args_list[0].kwargs["tools"]},
            architect_agent.ARCHITECT_PROFILE.allowed_tools,
        )
        self.assertEqual(
            [message["role"] for message in chat.call_args_list[1].kwargs["messages"]],
            ["system", "user", "assistant", "tool"],
        )

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
