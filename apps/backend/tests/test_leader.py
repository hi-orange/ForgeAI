import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.agents import leader as leader_agent
from app.agents.roles import LEADER_PROFILE
from app.core.exceptions import BusinessException
from app.schemas.agent_action import ChatWithToolsResult, ToolCall
from app.schemas.leader import LeaderContext
from app.tools.leader import LeaderToolState, execute_leader_tool


def leader_context(*, plans=None) -> LeaderContext:
    return LeaderContext.model_validate(
        {
            "project_id": 7,
            "project_name": "Reading tracker",
            "project_status": "draft",
            "run_id": "run_leader",
            "run_status": "running",
            "target_message": {
                "id": 2,
                "sequence": 2,
                "sender": "user",
                "content": "做一个阅读记录应用",
            },
            "recent_messages": [
                {
                    "id": 1,
                    "sequence": 1,
                    "sender": "assistant",
                    "content": "请描述应用",
                }
            ],
            "plans": plans or [],
        }
    )


def product_plan(*, version: int = 1) -> dict:
    return {
        "version": version,
        "cause_message_id": 2,
        "tasks": [
            {
                "task_key": "requirements",
                "recipient": "Product Manager",
                "title": "整理产品需求",
                "instructions": "根据目标消息生成可审批 PRD",
                "expected_output_type": "app_spec",
                "input_configuration_item_ids": [],
                "depends_on_task_keys": [],
            }
        ],
    }


def intent(*, category: str = "product_change", priority: str = "normal") -> dict:
    return {
        "category": category,
        "priority": priority,
        "summary": "用户提出了新的应用构建需求。",
    }


class LeaderTests(unittest.TestCase):
    def test_role_goal_and_tools_match_management_boundary(self):
        self.assertIn("判断意图和优先级", LEADER_PROFILE.goal)
        self.assertIn("持续跟踪计划", LEADER_PROFILE.goal)
        self.assertEqual(
            LEADER_PROFILE.allowed_tools,
            frozenset(
                {
                    "read_project_context",
                    "classify_intent",
                    "read_plan",
                    "create_plan",
                    "update_plan",
                    "dispatch_task",
                    "read_task_result",
                    "request_user_input",
                    "finish_turn",
                }
            ),
        )
        self.assertNotIn("apply_patch", LEADER_PROFILE.allowed_tools)
        self.assertNotIn("write_prd", LEADER_PROFILE.allowed_tools)

    def test_context_rejects_future_or_non_user_target_messages(self):
        base = leader_context().model_dump(mode="json")
        invalid = [
            {
                **base,
                "target_message": {**base["target_message"], "sender": "assistant"},
            },
            {
                **base,
                "recent_messages": [{**base["recent_messages"][0], "sequence": 3}],
            },
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                LeaderContext.model_validate(payload)

    def test_leader_creates_plan_dispatches_role_and_finishes_turn(self):
        state = LeaderToolState(context=leader_context())
        classified, _ = execute_leader_tool(
            ToolCall(
                id="intent",
                name="classify_intent",
                arguments={"intent": intent()},
            ),
            state,
        )
        self.assertTrue(classified.ok)
        created, _ = execute_leader_tool(
            ToolCall(
                id="plan",
                name="create_plan",
                arguments={"plan": product_plan()},
            ),
            state,
        )
        self.assertTrue(created.ok)
        assigned, _ = execute_leader_tool(
            ToolCall(
                id="dispatch",
                name="dispatch_task",
                arguments={"task_key": "requirements"},
            ),
            state,
        )
        self.assertTrue(assigned.ok)
        self.assertEqual(assigned.data["recipient"], "Product Manager")
        finished, outcome = execute_leader_tool(
            ToolCall(
                id="finish",
                name="finish_turn",
                arguments={"action": "dispatch", "summary": "已安排 Product Manager。"},
            ),
            state,
        )
        self.assertTrue(finished.ok)
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.action, "dispatch")
        self.assertEqual(outcome.dispatched_task_keys, ["requirements"])
        self.assertEqual(outcome.plan, state.draft)

    def test_leader_tracks_results_before_continuing_or_finishing(self):
        plans = [
            {
                "plan_id": "plan_1",
                "version": 1,
                "status": "running",
                "tasks": [
                    {
                        "task_id": "task_pm",
                        "task_key": "requirements",
                        "recipient": "Product Manager",
                        "title": "整理需求",
                        "status": "succeeded",
                        "depends_on_task_ids": [],
                        "result": {"item_id": "ci_app_spec"},
                    },
                    {
                        "task_id": "task_architect",
                        "task_key": "system_design",
                        "recipient": "Architect",
                        "title": "设计系统",
                        "status": "pending",
                        "depends_on_task_ids": ["task_pm"],
                        "result": None,
                    },
                ],
            }
        ]
        state = LeaderToolState(context=leader_context(plans=plans))
        execute_leader_tool(
            ToolCall(
                id="intent",
                name="classify_intent",
                arguments={"intent": intent(category="implementation_repair", priority="high")},
            ),
            state,
        )
        result, _ = execute_leader_tool(
            ToolCall(
                id="result",
                name="read_task_result",
                arguments={"task_id": "task_pm"},
            ),
            state,
        )
        self.assertEqual(result.data["task"]["result"]["item_id"], "ci_app_spec")
        assigned, _ = execute_leader_tool(
            ToolCall(
                id="dispatch",
                name="dispatch_task",
                arguments={"task_key": "system_design"},
            ),
            state,
        )
        self.assertTrue(assigned.ok)
        _, continued = execute_leader_tool(
            ToolCall(
                id="continue",
                name="finish_turn",
                arguments={"action": "continue", "summary": "继续系统设计。"},
            ),
            state,
        )
        self.assertIsNotNone(continued)
        assert continued is not None
        self.assertEqual(continued.action, "continue")
        self.assertIsNone(continued.plan)

        blocked_finish, blocked_outcome = execute_leader_tool(
            ToolCall(
                id="finish_early",
                name="finish_turn",
                arguments={"action": "finish", "summary": "完成。"},
            ),
            state,
        )
        self.assertFalse(blocked_finish.ok)
        self.assertIsNone(blocked_outcome)

    def test_plan_rejects_role_output_mismatch_and_persisted_version_rewrite(self):
        state = LeaderToolState(context=leader_context())
        execute_leader_tool(
            ToolCall(
                id="intent",
                name="classify_intent",
                arguments={"intent": intent()},
            ),
            state,
        )
        mismatch = product_plan()
        mismatch["tasks"][0]["expected_output_type"] = "code"
        rejected, _ = execute_leader_tool(
            ToolCall(id="bad", name="create_plan", arguments={"plan": mismatch}), state
        )
        self.assertFalse(rejected.ok)

        existing = leader_context(
            plans=[
                {
                    "plan_id": "plan_1",
                    "version": 1,
                    "status": "succeeded",
                    "tasks": [],
                }
            ]
        )
        state = LeaderToolState(context=existing)
        execute_leader_tool(
            ToolCall(
                id="intent_2",
                name="classify_intent",
                arguments={"intent": intent()},
            ),
            state,
        )
        rewrite, _ = execute_leader_tool(
            ToolCall(
                id="rewrite",
                name="create_plan",
                arguments={"plan": product_plan(version=1)},
            ),
            state,
        )
        self.assertFalse(rewrite.ok)

    def test_agent_runs_bounded_plan_and_dispatch_loop(self):
        actions = [
            ToolCall(id="context", name="read_project_context", arguments={}),
            ToolCall(
                id="intent",
                name="classify_intent",
                arguments={"intent": intent()},
            ),
            ToolCall(
                id="plan",
                name="create_plan",
                arguments={"plan": product_plan()},
            ),
            ToolCall(
                id="dispatch",
                name="dispatch_task",
                arguments={"task_key": "requirements"},
            ),
            ToolCall(
                id="finish",
                name="finish_turn",
                arguments={"action": "dispatch", "summary": "开始需求整理。"},
            ),
        ]
        turns = [ChatWithToolsResult(tool_calls=[action]) for action in actions]
        with patch.object(leader_agent, "chat_with_tools", side_effect=turns) as chat:
            outcome = leader_agent.lead_project_turn(leader_context())
        self.assertEqual(outcome.action, "dispatch")
        self.assertEqual(outcome.intent.priority, "normal")
        self.assertEqual(chat.call_count, 5)
        self.assertEqual(
            {tool.name for tool in chat.call_args_list[0].kwargs["tools"]},
            LEADER_PROFILE.allowed_tools,
        )
        self.assertEqual(
            [len(call.kwargs["messages"]) for call in chat.call_args_list],
            [2, 4, 6, 8, 10],
        )

    def test_agent_rejects_tools_outside_leader_role(self):
        with patch.object(
            leader_agent,
            "chat_with_tools",
            return_value=ChatWithToolsResult(
                tool_calls=[ToolCall(id="code", name="apply_patch", arguments={})]
            ),
        ):
            with self.assertRaisesRegex(BusinessException, "无权使用工具"):
                leader_agent.lead_project_turn(leader_context())

    def test_agent_defers_extra_parallel_tool_calls(self):
        turns = [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(id="context", name="read_project_context", arguments={}),
                    ToolCall(id="plan", name="read_plan", arguments={}),
                ]
            ),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(id="intent", name="classify_intent", arguments={"intent": intent()})
                ]
            ),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(id="create", name="create_plan", arguments={"plan": product_plan()})
                ]
            ),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="dispatch", name="dispatch_task", arguments={"task_key": "requirements"}
                    )
                ]
            ),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish_turn",
                        arguments={"action": "dispatch", "summary": "开始需求整理。"},
                    )
                ]
            ),
        ]
        with patch.object(leader_agent, "chat_with_tools", side_effect=turns) as chat:
            outcome = leader_agent.lead_project_turn(leader_context())
        self.assertEqual(outcome.action, "dispatch")
        second_turn_history = chat.call_args_list[1].kwargs["messages"]
        self.assertEqual(
            [message["role"] for message in second_turn_history],
            ["system", "user", "assistant", "tool", "tool"],
        )


if __name__ == "__main__":
    unittest.main()
