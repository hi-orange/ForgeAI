import json
import unittest
from unittest.mock import patch

from app.agents.element_editor import suggest_element_patch
from app.core.exceptions import BusinessException
from app.schemas.project import ProjectElementAiEdit


class ElementEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = ProjectElementAiEdit(
            element_id="forge-h1-1",
            tag_name="h1",
            text="原始标题",
            text_editable=True,
            styles={"color": "#111111", "font-size": "32px"},
            instruction="改成橙色，并让标题更有吸引力",
        )

    @patch("app.agents.element_editor.chat_completion")
    def test_returns_valid_patch_for_selected_element(self, completion) -> None:
        completion.return_value = json.dumps(
            {"text": "发现你的理想工作", "styles": {"color": "#F59E0B"}}
        )

        result = suggest_element_patch(self.payload)

        self.assertEqual(result.element_id, "forge-h1-1")
        self.assertEqual(result.changes.text, "发现你的理想工作")
        self.assertEqual(result.changes.styles["color"], "#F59E0B")

    @patch("app.agents.element_editor.chat_completion")
    def test_rejects_invalid_ai_style(self, completion) -> None:
        completion.return_value = json.dumps({"text": None, "styles": {"color": "orange"}})

        with self.assertRaisesRegex(BusinessException, "格式无效"):
            suggest_element_patch(self.payload)

    @patch("app.agents.element_editor.chat_completion")
    def test_rejects_text_change_for_container(self, completion) -> None:
        completion.return_value = json.dumps({"text": "替换容器", "styles": {}})
        payload = self.payload.model_copy(update={"text_editable": False})

        with self.assertRaisesRegex(BusinessException, "包含子元素"):
            suggest_element_patch(payload)


if __name__ == "__main__":
    unittest.main()
