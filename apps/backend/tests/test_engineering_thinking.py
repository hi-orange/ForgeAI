"""Thinking-mode transport and recoverable native history, without model calls."""

import json
import unittest
from unittest.mock import patch

from test_delivery_dependencies import dependency_spec

from app.agents.code_engineer import build_code_engineer_messages
from app.agents.tool_protocol import append_tool_batch_exchange
from app.core.exceptions import ModelProtocolException
from app.core.llm import chat_with_tools
from app.generation.delivery import planning_item
from app.generation.engineering.checkpoint import load_checkpoint, new_checkpoint
from app.schemas.agent_action import ChatWithToolsResult, ToolCall, ToolExecutionResult


def response_payload():
    return {
        "model": "deepseek-flash",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "reasoning_content": "opaque-provider-state",
                    "tool_calls": [
                        {
                            "id": "call",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"x.py"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


class EngineeringThinkingTests(unittest.TestCase):
    def test_thinking_request_and_legacy_history_without_mutating_input(self):
        messages = [{"role": "assistant", "content": "", "tool_calls": []}]
        with patch("app.core.llm._post_chat", return_value=response_payload()) as provider:
            result = chat_with_tools(messages=messages, tools=[], thinking=True)
        body = provider.call_args.args[0]
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertNotIn("temperature", body)
        self.assertEqual(body["messages"][0]["reasoning_content"], "")
        self.assertNotIn("reasoning_content", messages[0])
        self.assertEqual(result.reasoning_content, "opaque-provider-state")
        self.assertNotIn("opaque-provider-state", repr(result))

    def test_default_stays_nonthinking_and_missing_reasoning_is_rejected_when_enabled(self):
        payload = response_payload()
        del payload["choices"][0]["message"]["reasoning_content"]
        with patch("app.core.llm._post_chat", return_value=payload) as provider:
            chat_with_tools(messages=[], tools=[])
            self.assertEqual(provider.call_args.args[0]["thinking"], {"type": "disabled"})
            with self.assertRaisesRegex(ModelProtocolException, "reasoning_content"):
                chat_with_tools(messages=[], tools=[], thinking=True)

    def test_shared_exchange_places_reasoning_on_assistant_only(self):
        result = ChatWithToolsResult(
            reasoning_content="opaque", tool_calls=[ToolCall(id="a", name="read_file")]
        )
        observation = ToolExecutionResult(tool_call_id="a", name="read_file", ok=True, summary="ok")
        messages = []
        append_tool_batch_exchange(
            messages, result=result, calls=result.tool_calls, observations=[observation]
        )
        self.assertEqual(messages[0]["reasoning_content"], "opaque")
        self.assertNotIn("opaque", messages[1]["content"])

    def test_checkpoint_replays_complete_batch_without_compacting_reasoning(self):
        reasoning = "opaque" * 2000
        result = ChatWithToolsResult(
            reasoning_content=reasoning,
            tool_calls=[
                ToolCall(id="a", name="read_file", arguments={"path": "a.py"}),
                ToolCall(id="b", name="read_file", arguments={"path": "b.py"}),
            ],
        )
        checkpoint = new_checkpoint()
        checkpoint.update(
            outcome="blocked",
            model_turns=5,
            thinking_enabled=True,
            model_responses=[{"model_turn": 5, "result": result.model_dump(mode="json")}],
        )
        checkpoint = load_checkpoint({"checkpoint": json.loads(json.dumps(checkpoint))})
        self.assertEqual(checkpoint["outcome"], "blocked")
        self.assertEqual(checkpoint["model_turns"], 5)
        observations = [
            ToolExecutionResult(
                tool_call_id=call.id,
                model_turn=5,
                name=call.name,
                ok=True,
                summary="read",
                arguments=call.arguments,
                data={
                    "path": call.arguments["path"],
                    "content_hash": "hash",
                    "start_line": 1,
                    "end_line": 1,
                },
            )
            for call in result.tool_calls
        ]
        spec = dependency_spec()
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            engineering_context={"model_responses": checkpoint["model_responses"]},
            observations=observations,
            system_design=None,
        )
        assistant = [m for m in messages if m["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertEqual(len(assistant[0]["tool_calls"]), 2)
        self.assertEqual(assistant[0]["reasoning_content"], reasoning)
        self.assertEqual(len([m for m in messages if m["role"] == "tool"]), 2)
        self.assertNotIn("opaque", messages[-1]["content"])
        # An incomplete recovered batch must not fabricate the missing result.
        partial = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            engineering_context={"model_responses": checkpoint["model_responses"]},
            observations=observations[:1],
            system_design=None,
        )
        self.assertFalse(any(m["role"] == "assistant" for m in partial))

    def test_reused_call_ids_do_not_mix_provider_reasoning_between_turns(self):
        responses = []
        observations = []
        for turn in (1, 2):
            result = ChatWithToolsResult(
                reasoning_content=f"opaque-{turn}",
                tool_calls=[
                    ToolCall(
                        id="same",
                        name="write_file",
                        arguments={"path": f"{turn}.py", "content": "x"},
                    )
                ],
            )
            responses.append({"model_turn": turn, "result": result.model_dump(mode="json")})
            observations.append(
                ToolExecutionResult(
                    tool_call_id="same",
                    model_turn=turn,
                    name="write_file",
                    ok=True,
                    summary="saved",
                    arguments=result.tool_calls[0].arguments,
                )
            )
        spec = dependency_spec()
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            engineering_context={"model_responses": responses},
            observations=observations,
            system_design=None,
        )
        assistants = [m for m in messages if m["role"] == "assistant"]
        self.assertEqual([m["reasoning_content"] for m in assistants], ["opaque-1", "opaque-2"])
        self.assertEqual(
            [json.loads(m["tool_calls"][0]["function"]["arguments"])["path"] for m in assistants],
            ["1.py", "2.py"],
        )
