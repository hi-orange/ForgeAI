"""LLM provider HTTP error mapping."""

from __future__ import annotations

import json
import logging
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine

from app.core.exceptions import ModelProtocolException, ProviderException
from app.core.llm import _http_error_message, _http_error_retryable, chat_with_tools
from app.core.llm_response import CompletionContentError, normalize_completion_content
from app.core.settings import settings
from app.telemetry.agent import agent_telemetry_scope


def _response(status_code: int, text: str) -> httpx.Response:
    return httpx.Response(status_code, text=text)


def _stream_chunk(
    delta: dict, *, finish_reason: str | None = None, usage: dict | None = None
) -> dict:
    return {
        "id": "chat_1",
        "model": "deepseek-flash",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        "usage": usage,
    }


def _sse(*chunks: dict, done: bool = True) -> bytes:
    events = [": keep-alive\n\n"]
    events.extend(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
    if done:
        events.append("data: [DONE]\n\n")
    return "".join(events).encode("utf-8")


class LlmHttpErrorMessageTests(unittest.TestCase):
    def test_provider_error_logs_include_scoped_identity_without_leaking_scope(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        response = httpx.Response(
            503, text="busy", request=httpx.Request("POST", "https://fixture.invalid")
        )
        with (
            patch.object(settings, "deepseek_api_key", "fixture-key"),
            patch("app.core.llm.httpx.Client") as client,
            self.assertLogs("forgeai.llm", level="WARNING") as logs,
        ):
            client.return_value.__enter__.return_value.post.return_value = response
            with agent_telemetry_scope(
                project_id=7,
                build_run_id="run_7",
                task_id="task_7",
                execution_id="exec_7",
                role="Architect",
                bind=engine,
            ):
                with self.assertRaises(ProviderException):
                    chat_with_tools(messages=[], tools=[])
            logging.getLogger("forgeai.llm").warning("unscoped")
        for field in (
            "project=7",
            "run=run_7",
            "task=task_7",
            "execution=exec_7",
            "role=Architect",
        ):
            self.assertIn(field, logs.output[0])
        self.assertNotIn("fixture-key", logs.output[0])
        self.assertNotIn("run=run_7", logs.output[1])

    def test_provider_failure_has_service_status_not_client_error(self):
        self.assertEqual(ProviderException("network", retryable=True).status_code, 503)
        self.assertEqual(ProviderException("config", retryable=False).status_code, 502)

    def test_length_response_is_rejected_even_if_first_tool_json_is_complete(self):
        payload = {
            "model": "test",
            "usage": {"total_tokens": 120},
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "partial_batch",
                                "type": "function",
                                "function": {"name": "editor_write", "arguments": "{}"},
                            }
                        ]
                    },
                }
            ],
        }
        with (
            patch("app.core.llm._post_chat", return_value=payload),
            patch("app.core.llm.record_model_turn") as meter,
        ):
            with self.assertRaisesRegex(ModelProtocolException, "MODEL_OUTPUT_TRUNCATED"):
                chat_with_tools(messages=[], tools=[])
        meter.assert_called_once()

    def test_invalid_tool_arguments_are_metered_before_protocol_failure(self):
        payload = {
            "model": "test-model",
            "usage": {"total_tokens": 120},
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "bad",
                                "type": "function",
                                "function": {"name": "editor_write", "arguments": '{"unfinished":'},
                            }
                        ]
                    },
                }
            ],
        }
        with (
            patch("app.core.llm._post_chat", return_value=payload),
            patch("app.core.llm.record_model_turn") as meter,
            self.assertLogs("forgeai.llm", level="WARNING") as logs,
        ):
            with self.assertRaises(ModelProtocolException):
                chat_with_tools(messages=[], tools=[])
        meter.assert_called_once()
        self.assertEqual(meter.call_args.args[0].usage.total_tokens, 120)
        self.assertIn("finish_reason=length", logs.output[0])

    def test_malformed_native_arguments_keep_safe_tool_identity_for_correction(self):
        raw = '{"writes":[{"path":"private.py","content":"secret",}]}'
        payload = {
            "model": "test-model",
            "usage": {"total_tokens": 120},
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "bad_write",
                                "type": "function",
                                "function": {"name": "write_files", "arguments": raw},
                            }
                        ]
                    },
                }
            ],
        }
        with (
            patch("app.core.llm._post_chat", return_value=payload),
            patch("app.core.llm.record_model_turn") as meter,
        ):
            with self.assertRaises(ModelProtocolException) as raised:
                chat_with_tools(messages=[], tools=[])
        self.assertEqual(meter.call_args.args[0].usage.total_tokens, 120)
        self.assertEqual(raised.exception.data["protocol_issue"], "invalid_tool_arguments")
        self.assertEqual(raised.exception.data["tool_name"], "write_files")
        self.assertEqual(raised.exception.data["tool_call_id"], "bad_write")
        self.assertEqual(raised.exception.data["argument_length"], len(raw))
        self.assertNotIn("secret", str(raised.exception.data))

    def test_maps_insufficient_balance_402(self) -> None:
        message = _http_error_message(
            _response(402, '{"error":{"message":"Insufficient Balance","type":"unknown_error"}}')
        )
        self.assertIn("余额不足", message)

    def test_maps_insufficient_balance_body_without_relying_on_status(self) -> None:
        message = _http_error_message(_response(400, "Error: insufficient_balance"))
        self.assertIn("余额不足", message)

    def test_maps_auth_failure(self) -> None:
        message = _http_error_message(_response(401, '{"error":"Invalid API key"}'))
        self.assertIn("鉴权失败", message)

    def test_maps_rate_limit(self) -> None:
        message = _http_error_message(_response(429, "Rate limit exceeded"))
        self.assertIn("过于频繁", message)

    def test_maps_generic_client_error(self) -> None:
        message = _http_error_message(_response(400, "bad request"))
        self.assertEqual(message, "大模型调用失败，请稍后重试")

    def test_permanent_provider_errors_are_not_retryable(self) -> None:
        self.assertFalse(_http_error_retryable(_response(402, "Insufficient Balance")))
        self.assertFalse(_http_error_retryable(_response(401, "Invalid API key")))
        self.assertFalse(_http_error_retryable(_response(400, "bad request")))

    def test_transient_provider_errors_remain_retryable(self) -> None:
        self.assertTrue(_http_error_retryable(_response(429, "Rate limit exceeded")))
        self.assertTrue(_http_error_retryable(_response(503, "unavailable")))

    def test_provider_adapter_removes_outer_final_envelope(self) -> None:
        self.assertEqual(
            normalize_completion_content(
                "<final>\nexport const value = 1\n</final>", kind="source"
            ),
            "export const value = 1",
        )
        self.assertEqual(
            normalize_completion_content("export const value = 1\n</final>", kind="source"),
            "export const value = 1",
        )

    def test_provider_adapter_rejects_embedded_source_protocol(self) -> None:
        with self.assertRaises(CompletionContentError):
            normalize_completion_content("export const value = 1\n</final>\nmore", kind="source")
        with self.assertRaises(CompletionContentError):
            normalize_completion_content("export const value = 1\n</parameter>", kind="source")
        with self.assertRaises(CompletionContentError):
            normalize_completion_content(
                '<invoke name="write_file"><parameter name="content">x',
                kind="source",
            )

    def test_provider_adapter_keeps_normal_tsx_elements(self) -> None:
        content = (
            'const example = "</parameter>"\nexport const Page = () => <main><Parameter /></main>'
        )
        self.assertEqual(normalize_completion_content(content, kind="source"), content)


class LlmThinkingStreamTests(unittest.TestCase):
    def _call(self, response: httpx.Response):
        requests: list[dict] = []

        def send(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return response

        client = httpx.Client(transport=httpx.MockTransport(send))
        with (
            patch.object(settings, "deepseek_api_key", "fixture-key"),
            patch("app.core.llm.httpx.Client", return_value=client),
        ):
            return chat_with_tools(
                messages=[{"role": "user", "content": "next"}], tools=[], thinking=True
            ), requests

    def test_assembles_reasoning_and_interleaved_tool_calls_before_metering(self):
        content = _sse(
            _stream_chunk({"role": "assistant", "reasoning_content": "plan "}),
            _stream_chunk({"reasoning_content": "steps", "content": "ready"}),
            _stream_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_b",
                            "type": "function",
                            "function": {"name": "second", "arguments": '{"b":'},
                        }
                    ]
                }
            ),
            _stream_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "first", "arguments": '{"a":'},
                        }
                    ]
                }
            ),
            _stream_chunk({"tool_calls": [{"index": 1, "function": {"arguments": "2}"}}]}),
            _stream_chunk({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}),
            _stream_chunk(
                {},
                finish_reason="tool_calls",
                usage={"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16},
            ),
        )
        with patch("app.core.llm.record_model_turn") as meter:
            result, requests = self._call(
                httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})
            )
        self.assertEqual(result.reasoning_content, "plan steps")
        self.assertEqual(result.content, "ready")
        self.assertEqual([call.name for call in result.tool_calls], ["first", "second"])
        self.assertEqual([call.arguments for call in result.tool_calls], [{"a": 1}, {"b": 2}])
        self.assertEqual(result.protocol, "native")
        self.assertEqual(result.usage.total_tokens, 16)
        meter.assert_called_once()
        self.assertEqual(meter.call_args.args[0].usage.total_tokens, 16)
        self.assertTrue(requests[0]["stream"])
        self.assertEqual(requests[0]["stream_options"], {"include_usage": True})
        self.assertEqual(requests[0]["thinking"], {"type": "enabled"})

    def test_missing_done_rejects_partial_tool_call_without_metering(self):
        content = _sse(
            _stream_chunk({"reasoning_content": "thinking"}),
            _stream_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "write_files", "arguments": "{}"},
                        }
                    ]
                }
            ),
            _stream_chunk({}, finish_reason="tool_calls", usage={"total_tokens": 20}),
            done=False,
        )
        with patch("app.core.llm.record_model_turn") as meter:
            with self.assertRaises(ProviderException) as raised:
                self._call(httpx.Response(200, content=content))
        self.assertTrue(raised.exception.retryable)
        meter.assert_not_called()

    def test_midstream_remote_close_is_retryable(self):
        class Interrupted(httpx.SyncByteStream):
            def __iter__(self):
                yield _sse(_stream_chunk({"reasoning_content": "thinking"}), done=False)
                raise httpx.RemoteProtocolError(
                    "peer closed connection without sending complete message body"
                )

        with patch("app.core.llm.record_model_turn") as meter:
            with self.assertRaises(ProviderException) as raised:
                self._call(httpx.Response(200, stream=Interrupted()))
        self.assertTrue(raised.exception.retryable)
        meter.assert_not_called()

    def test_length_terminal_is_metered_then_rejected(self):
        content = _sse(
            _stream_chunk(
                {
                    "reasoning_content": "unfinished",
                    "tool_calls": [{"index": 0, "function": {"arguments": "{"}}],
                }
            ),
            _stream_chunk(
                {},
                finish_reason="length",
                usage={
                    "completion_tokens": 8,
                    "total_tokens": 12,
                    "completion_tokens_details": {"reasoning_tokens": 8},
                },
            ),
        )
        with patch("app.core.llm.record_model_turn") as meter:
            with self.assertRaisesRegex(ModelProtocolException, "MODEL_OUTPUT_TRUNCATED"):
                self._call(httpx.Response(200, content=content))
        self.assertEqual(meter.call_args.args[0].usage.total_tokens, 12)

    def test_stream_http_error_keeps_existing_retry_classification(self):
        with self.assertRaises(ProviderException) as raised:
            self._call(httpx.Response(402, text="Insufficient Balance"))
        self.assertFalse(raised.exception.retryable)


if __name__ == "__main__":
    unittest.main()
