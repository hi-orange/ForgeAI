"""LLM provider HTTP error mapping."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from app.core.exceptions import ModelProtocolException, ProviderException
from app.core.llm import _http_error_message, _http_error_retryable, chat_with_tools
from app.core.llm_response import CompletionContentError, normalize_completion_content


def _response(status_code: int, text: str) -> httpx.Response:
    return httpx.Response(status_code, text=text)


class LlmHttpErrorMessageTests(unittest.TestCase):
    def test_provider_failure_has_service_status_not_client_error(self):
        self.assertEqual(ProviderException("network", retryable=True).status_code, 503)
        self.assertEqual(ProviderException("config", retryable=False).status_code, 502)

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


if __name__ == "__main__":
    unittest.main()
