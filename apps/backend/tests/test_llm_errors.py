"""LLM provider HTTP error mapping."""

from __future__ import annotations

import unittest

import httpx

from app.core.llm import _http_error_message
from app.core.llm_response import CompletionContentError, normalize_completion_content


def _response(status_code: int, text: str) -> httpx.Response:
    return httpx.Response(status_code, text=text)


class LlmHttpErrorMessageTests(unittest.TestCase):
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
