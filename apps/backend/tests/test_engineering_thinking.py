"""Thinking-mode transport and recoverable native history, without model calls."""

import json
import unittest
from unittest.mock import patch

from test_delivery_dependencies import dependency_spec

from app.agents.code_engineer import build_code_engineer_messages, decide_next_action
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

    def test_parallel_tool_calls_opt_in_keeps_other_roles_serial(self):
        with patch("app.core.llm._post_chat", return_value=response_payload()) as provider:
            chat_with_tools(messages=[], tools=[])
            self.assertIs(provider.call_args.args[0]["parallel_tool_calls"], False)
            chat_with_tools(messages=[], tools=[], parallel_tool_calls=True)
            self.assertIs(provider.call_args.args[0]["parallel_tool_calls"], True)

    def test_code_engineer_opts_in_to_parallel_tool_calls(self):
        spec = dependency_spec()
        result = ChatWithToolsResult(tool_calls=[ToolCall(id="read", name="read_file")])
        with patch("app.agents.code_engineer.chat_with_tools", return_value=result) as provider:
            decide_next_action(
                spec=spec,
                work_item=planning_item(spec),
                engineering_context={},
                observations=[],
                system_design=None,
            )
        self.assertIs(provider.call_args.kwargs["parallel_tool_calls"], True)

    def test_unavailable_tool_reports_the_actual_attempted_call(self):
        spec = dependency_spec()
        result = ChatWithToolsResult(
            tool_calls=[ToolCall(id="unavailable", name="read_file", arguments={"path": "x.py"})]
        )
        with patch("app.agents.code_engineer.chat_with_tools", return_value=result):
            with self.assertRaises(ModelProtocolException) as caught:
                decide_next_action(
                    spec=spec,
                    work_item=planning_item(spec),
                    engineering_context={},
                    observations=[],
                    system_design=None,
                    tools=[],
                )
        self.assertEqual(
            caught.exception.data["rejected_tool_call"], result.tool_calls[0].model_dump()
        )

    def test_inspection_gate_replays_rejected_read_but_not_old_successful_reads(self):
        spec = dependency_spec()
        observations = [
            ToolExecutionResult(
                tool_call_id="old",
                model_turn=1,
                name="read_file",
                ok=True,
                summary="read",
                arguments={"path": "old.py"},
                data={
                    "path": "old.py",
                    "content_hash": "hash",
                    "start_line": 1,
                    "end_line": 1,
                    "content": "VALUE = 1",
                },
            ),
            ToolExecutionResult(
                tool_call_id="rejected",
                model_turn=2,
                name="read_file",
                ok=False,
                error_code="INSPECTION_ACTION_REQUIRED",
                summary="read not executed",
                arguments={"path": "next.py"},
            ),
        ]
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            engineering_context={
                "available_tools": ["write_files", "update_delivery_plan", "report_blocked"],
                "working_memory": {"source_evidence": [observations[0].data]},
                "progress_guidance": {"inspection_action_required": True},
            },
            observations=observations,
            system_design=None,
        )
        calls = [
            call
            for message in messages
            if message["role"] == "assistant"
            for call in message["tool_calls"]
        ]
        self.assertEqual([call["id"] for call in calls], ["rejected"])
        tool_results = [
            json.loads(message["content"]) for message in messages if message["role"] == "tool"
        ]
        self.assertEqual(len(tool_results), 1)
        self.assertFalse(tool_results[0]["ok"])
        self.assertEqual(tool_results[0]["error_code"], "INSPECTION_ACTION_REQUIRED")
        self.assertIn("本轮不能调用 read_file", messages[-1]["content"])
        self.assertIn('"write_files"', messages[-1]["content"])

    def test_malformed_action_retry_does_not_replay_reads_as_available_tools(self):
        spec = dependency_spec()
        read = ToolExecutionResult(
            tool_call_id="read",
            model_turn=1,
            name="read_file",
            ok=True,
            summary="read",
            arguments={"path": "backend/app/api/router.py"},
            data={
                "path": "backend/app/api/router.py",
                "content_hash": "current",
                "start_line": 1,
                "end_line": 1,
                "content": "router = APIRouter()",
            },
        )
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            engineering_context={
                "available_tools": ["write_files", "edit_file_by_replace", "report_blocked"],
                "working_memory": {"source_evidence": [read.data]},
                "protocol_retry": {"tool_name": "write_files", "model_turn": 2},
            },
            observations=[read],
            system_design=None,
        )
        self.assertFalse(any(message["role"] == "assistant" for message in messages))
        self.assertIn("write_files", messages[-1]["content"])
        self.assertIn("工具参数不是合法 JSON", messages[-1]["content"])
        self.assertIn("router = APIRouter()", messages[-2]["content"])

    def test_unchanged_failed_check_guides_source_repair_without_check_tools(self):
        spec = dependency_spec()
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            engineering_context={
                "available_tools": ["edit_file_by_replace", "report_blocked"],
                "failed_check_repair": {"check_ids": ["all"], "source_hash": "same"},
                "active_diagnostics": [{"scope": "check:all"}],
            },
            observations=[],
            system_design=None,
        )
        self.assertIn("不提供 run_check 或 complete_work_item", messages[-1]["content"])
        self.assertIn('"all"', messages[-1]["content"])
        self.assertIn("edit_file_by_replace", messages[-1]["content"])

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

    def test_read_history_keeps_newest_complete_batches_and_current_evidence(self):
        spec = dependency_spec()
        responses = []
        observations = []
        for turn in (1, 2, 3):
            result = ChatWithToolsResult(
                reasoning_content=f"reasoning-{turn}",
                tool_calls=[
                    ToolCall(
                        id=f"{turn}-{suffix}",
                        name="read_file",
                        arguments={"path": f"{turn}-{suffix}.py"},
                    )
                    for suffix in ("a", "b")
                ],
            )
            responses.append({"model_turn": turn, "result": result.model_dump(mode="json")})
            observations.extend(
                ToolExecutionResult(
                    tool_call_id=call.id,
                    model_turn=turn,
                    name="read_file",
                    ok=True,
                    summary="read",
                    arguments=call.arguments,
                    data={
                        "path": call.arguments["path"],
                        "content_hash": "current",
                        "start_line": 1,
                        "end_line": 1,
                        "content": f"SOURCE_{turn}_{call.id}",
                    },
                )
                for call in result.tool_calls
            )
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            engineering_context={
                "model_responses": responses,
                "working_memory": {
                    "source_evidence": [
                        {"path": "1-a.py", "content_hash": "current", "content": "SOURCE_1_1-a"}
                    ]
                },
            },
            observations=observations,
            system_design=None,
        )
        assistants = [message for message in messages if message["role"] == "assistant"]
        tools = [message for message in messages if message["role"] == "tool"]
        self.assertEqual(
            [m["reasoning_content"] for m in assistants], ["reasoning-2", "reasoning-3"]
        )
        self.assertEqual([len(m["tool_calls"]) for m in assistants], [2, 2])
        self.assertEqual([m["tool_call_id"] for m in tools], ["2-a", "2-b", "3-a", "3-b"])
        self.assertIn("SOURCE_1_1-a", messages[-1]["content"])
        self.assertIn("SOURCE_3_3-b", tools[-1]["content"])

    def test_check_history_keeps_diagnostic_and_source_identity_without_full_log(self):
        spec = dependency_spec()
        output = "CHECK START\n" + "irrelevant log line\n" * 1200 + "CHECK END"
        diagnostic = {
            "code": "CHECK_FAILED",
            "issues": [{"code": "PUBLIC_ROUTE_MISSING", "actual": "GET /api/v1/jobs: 404"}],
        }
        for name in ("run_check", "complete_work_item"):
            with self.subTest(name=name):
                observation = ToolExecutionResult(
                    tool_call_id=f"check-{name}",
                    model_turn=3,
                    name=name,
                    ok=False,
                    error_code="CHECK_FAILED",
                    summary="integration check failed",
                    arguments={"check_id": "all"},
                    data={
                        "check_id": "all",
                        "source_hash": "exact-source-hash",
                        "diagnostic": diagnostic,
                        "output": output,
                    },
                )
                messages = build_code_engineer_messages(
                    spec=spec,
                    work_item=planning_item(spec),
                    engineering_context={},
                    observations=[observation],
                    system_design=None,
                )
                assistant = next(m for m in messages if m["role"] == "assistant")
                tool = next(m for m in messages if m["role"] == "tool")
                projected = json.loads(tool["content"])
                self.assertEqual(assistant["tool_calls"][0]["id"], tool["tool_call_id"])
                self.assertEqual(projected["data"]["check_id"], "all")
                self.assertEqual(projected["data"]["source_hash"], "exact-source-hash")
                self.assertEqual(projected["data"]["diagnostic"], diagnostic)
                self.assertEqual(projected["data"]["output_total_chars"], len(output))
                self.assertIn("CHECK START", projected["data"]["output"])
                self.assertIn("CHECK END", projected["data"]["output"])
                self.assertLess(len(projected["data"]["output"]), 1200)
                self.assertTrue(projected["truncated"])
                self.assertEqual(observation.data["output"], output)

    def test_short_check_output_is_not_marked_truncated_in_history(self):
        spec = dependency_spec()
        observation = ToolExecutionResult(
            tool_call_id="short-check",
            name="run_check",
            ok=True,
            summary="passed",
            data={"check_id": "quick", "source_hash": "source", "output": "all checks passed"},
        )
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            engineering_context={},
            observations=[observation],
            system_design=None,
        )
        projected = json.loads(next(m for m in messages if m["role"] == "tool")["content"])
        self.assertEqual(projected["data"]["output"], "all checks passed")
        self.assertNotIn("output_total_chars", projected["data"])
        self.assertFalse(projected["truncated"])

    def test_stale_read_does_not_displace_recent_current_read_history(self):
        spec = dependency_spec()
        observations = [
            ToolExecutionResult(
                tool_call_id=f"read-{turn}",
                model_turn=turn,
                name="read_file",
                ok=True,
                summary="read",
                arguments={"path": f"{turn}.py"},
                data={
                    "path": f"{turn}.py",
                    "content_hash": "old" if turn == 4 else "current",
                    "start_line": 1,
                    "end_line": 1,
                    "content": "x" * 6000,
                },
            )
            for turn in (1, 2, 3, 4)
        ]
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            engineering_context={
                "working_memory": {
                    "source_hashes": {"4.py": "current"},
                    "source_evidence": [],
                }
            },
            observations=observations,
            system_design=None,
        )
        tools = [message for message in messages if message["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tools], ["read-2", "read-3"])
        self.assertLess(sum(len(m["content"]) for m in messages), 20_000)

    def test_large_latest_read_batch_is_never_split_by_history_budget(self):
        spec = dependency_spec()
        response = ChatWithToolsResult(
            tool_calls=[
                ToolCall(id=f"read-{i}", name="read_file", arguments={"path": f"{i}.py"})
                for i in range(12)
            ]
        )
        observations = [
            ToolExecutionResult(
                tool_call_id=call.id,
                model_turn=7,
                name="read_file",
                ok=True,
                summary="read",
                arguments=call.arguments,
                data={
                    "path": call.arguments["path"],
                    "content_hash": "current",
                    "start_line": 1,
                    "end_line": 1,
                    "content": "x" * 10_000,
                },
            )
            for call in response.tool_calls
        ]
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            engineering_context={
                "model_responses": [{"model_turn": 7, "result": response.model_dump(mode="json")}]
            },
            observations=observations,
            system_design=None,
        )
        assistants = [m for m in messages if m["role"] == "assistant"]
        tools = [m for m in messages if m["role"] == "tool"]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(len(assistants[0]["tool_calls"]), 12)
        self.assertEqual(len(tools), 12)
        self.assertEqual([m["tool_call_id"] for m in tools], [f"read-{i}" for i in range(12)])

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
