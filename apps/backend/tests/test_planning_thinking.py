"""Optional thinking mode reaches each planning role's tool-loop model calls."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_architect import valid_spec
from test_leader import leader_context
from test_product_manager import model_input

from app.agents import architect, leader, product_manager
from app.agents.tool_protocol import run_bounded_tool_loop
from app.core.settings import Settings, settings
from app.schemas.agent_action import (
    ChatWithToolsResult,
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
)


class PlanningThinkingTests(unittest.TestCase):
    def test_environment_switches_are_independent(self):
        with patch.dict(
            "os.environ",
            {
                "LEADER_THINKING": "true",
                "PRODUCT_MANAGER_THINKING": "false",
                "ARCHITECT_THINKING": "true",
            },
        ):
            configured = Settings(_env_file=None)
        self.assertTrue(configured.leader_thinking)
        self.assertFalse(configured.product_manager_thinking)
        self.assertTrue(configured.architect_thinking)

    def test_tool_loop_forwards_opt_in_and_preserves_reasoning_between_turns(self):
        requests = []

        def call_model(**kwargs):
            requests.append(kwargs)
            name = "read" if len(requests) == 1 else "finish"
            return ChatWithToolsResult(
                reasoning_content=f"opaque-{name}",
                tool_calls=[ToolCall(id=name, name=name)],
            )

        def execute(call):
            return (
                ToolExecutionResult(
                    tool_call_id=call.id,
                    name=call.name,
                    ok=True,
                    summary="done",
                ),
                "complete" if call.name == "finish" else None,
            )

        output = run_bounded_tool_loop(
            messages=[{"role": "user", "content": "start"}],
            tools=[
                ToolDefinition(name=name, description=name, parameters={})
                for name in ("read", "finish")
            ],
            allowed_tools={"read", "finish"},
            role_name="planning",
            max_turns=2,
            temperature=0.0,
            max_tokens=4096,
            thinking=True,
            missing_message="missing",
            exhausted_message="exhausted",
            execute_tool=execute,
            model_call=call_model,
        )

        self.assertEqual(output, "complete")
        self.assertTrue(all(request["thinking"] is True for request in requests))
        self.assertEqual(requests[1]["messages"][1]["reasoning_content"], "opaque-read")

    def test_default_tool_loop_keeps_legacy_model_call_signature(self):
        def call_model(*, messages, tools, temperature, max_tokens):
            return ChatWithToolsResult(tool_calls=[ToolCall(id="finish", name="finish")])

        output = run_bounded_tool_loop(
            messages=[],
            tools=[ToolDefinition(name="finish", description="finish", parameters={})],
            allowed_tools={"finish"},
            role_name="planning",
            max_turns=1,
            temperature=0.0,
            max_tokens=256,
            missing_message="missing",
            exhausted_message="exhausted",
            execute_tool=lambda call: (
                ToolExecutionResult(tool_call_id=call.id, name=call.name, ok=True, summary="done"),
                "complete",
            ),
            model_call=call_model,
        )
        self.assertEqual(output, "complete")

    def test_product_manager_and_architect_use_their_own_settings(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                with (
                    patch.object(settings, "product_manager_thinking", enabled),
                    patch.object(product_manager, "run_bounded_tool_loop") as pm_loop,
                ):
                    product_manager.generate_app_spec(model_input())
                self.assertIs(pm_loop.call_args.kwargs["thinking"], enabled)
                self.assertEqual(pm_loop.call_args.kwargs["max_tokens"], 16384 if enabled else 8192)

                with TemporaryDirectory() as directory:
                    with (
                        patch.object(settings, "architect_thinking", enabled),
                        patch.object(architect, "run_bounded_tool_loop") as architect_loop,
                    ):
                        architect.generate_system_design(
                            spec=valid_spec(), workspace_root=Path(directory)
                        )
                self.assertIs(architect_loop.call_args.kwargs["thinking"], enabled)
                self.assertEqual(
                    architect_loop.call_args.kwargs["max_tokens"], 16384 if enabled else 8192
                )

    def test_leader_uses_setting_for_classification_and_management(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                with (
                    patch.object(settings, "leader_thinking", enabled),
                    patch.object(leader, "run_bounded_tool_loop") as loop,
                ):
                    leader.classify_message(
                        project_name="Demo",
                        project_status="draft",
                        recent_messages=[],
                        message_sequence=1,
                        message_content="Build an app",
                    )
                    leader.lead_project_turn(leader_context())
                self.assertEqual(loop.call_count, 2)
                classify_request, management_request = [call.kwargs for call in loop.call_args_list]
                self.assertIs(classify_request["thinking"], enabled)
                self.assertIs(management_request["thinking"], enabled)
                self.assertEqual(classify_request["max_tokens"], 4096 if enabled else 256)
                self.assertEqual(management_request["max_tokens"], 16384 if enabled else 8192)


if __name__ == "__main__":
    unittest.main()
