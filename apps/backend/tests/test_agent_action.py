"""Contract tests for tool-calling parse. No network."""

import unittest

from app.core.exceptions import BusinessException
from app.core.llm import parse_agent_action


class ParseAgentActionTests(unittest.TestCase):
    def test_native_tool_only_allows_empty_content(self):
        result = parse_agent_action(
            {
                "model": "deepseek-chat",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_17",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"backend/app/main.py"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            }
        )
        self.assertEqual(result.protocol, "native")
        self.assertEqual(result.tool_calls[0].name, "read_file")
        self.assertEqual(result.tool_calls[0].arguments["path"], "backend/app/main.py")

    def test_invalid_tool_arguments_json_is_rejected(self):
        with self.assertRaises(BusinessException):
            parse_agent_action(
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "function": {"name": "read_file", "arguments": "{bad"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            )

    def test_json_protocol_parses_single_action(self):
        result = parse_agent_action(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"name":"list_files","arguments":{"path":"frontend"}}'
                        },
                    }
                ]
            }
        )
        self.assertEqual(result.protocol, "json")
        self.assertEqual(result.tool_calls[0].name, "list_files")

    def test_empty_content_without_tools_fails(self):
        with self.assertRaises(BusinessException):
            parse_agent_action(
                {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
            )
