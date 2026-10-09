import unittest
from typing import Any

from app.tools.architect import ARCHITECT_TOOLS
from app.tools.code_engineer import CODE_ENGINEER_TOOLS
from app.tools.leader import LEADER_TOOLS, MESSAGE_CLASSIFICATION_TOOL
from app.tools.product_manager import PRODUCT_MANAGER_TOOLS
from app.tools.test_engineer import TEST_ENGINEER_TOOLS


def _schema_keywords(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).intersection({"$ref", "$defs"}) | {
            keyword for item in value.values() for keyword in _schema_keywords(item)
        }
    if isinstance(value, list):
        return {keyword for item in value for keyword in _schema_keywords(item)}
    return set()


class ToolSchemaCompatibilityTests(unittest.TestCase):
    def test_model_tool_schemas_do_not_contain_broken_local_references(self):
        tools = [
            MESSAGE_CLASSIFICATION_TOOL,
            *LEADER_TOOLS,
            *PRODUCT_MANAGER_TOOLS,
            *ARCHITECT_TOOLS,
            *CODE_ENGINEER_TOOLS,
            *TEST_ENGINEER_TOOLS,
        ]
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertEqual(_schema_keywords(tool.parameters), set())
