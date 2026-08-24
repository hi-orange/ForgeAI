import json
import unittest

from app.core.exceptions import BusinessException
from app.schemas.project import WebsiteElementChanges, WebsiteElementPatch
from app.services.website_editor import apply_website_patches, ensure_editable_ids


def website_files(html: str, css: str = "body { color: #111111; }") -> str:
    return json.dumps(
        {
            "index.html": html,
            "style.css": css,
            "script.js": "",
        },
        ensure_ascii=False,
    )


class WebsiteEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.files = website_files(
            '<!doctype html><html><head><meta charset="utf-8"><title>Editor</title></head>'
            "<body><main><div><h1>原始标题</h1><p>原始段落</p><button>提交</button></div></main>"
            "</body></html>"
        )

    def test_assigns_stable_unique_editable_ids(self) -> None:
        first = ensure_editable_ids(self.files)
        second = ensure_editable_ids(first)
        html = json.loads(first)["index.html"]

        self.assertEqual(first, second)
        self.assertIn('data-forge-id="forge-h1-1"', html)
        self.assertIn('data-forge-id="forge-p-1"', html)
        self.assertIn('data-forge-id="forge-button-1"', html)

    def test_applies_escaped_text_and_color_overrides(self) -> None:
        patch = WebsiteElementPatch(
            element_id="forge-h1-1",
            changes=WebsiteElementChanges(
                text="新的 <标题>",
                styles={
                    "color": "#123456",
                    "background-color": "#abcdef",
                    "font-size": "32px",
                    "margin-top": "12px",
                    "padding-left": "16px",
                    "border-radius": "8px",
                },
            ),
        )

        updated = json.loads(apply_website_patches(self.files, [patch]))

        self.assertIn("新的 &lt;标题&gt;", updated["index.html"])
        self.assertIn('[data-forge-id="forge-h1-1"]', updated["style.css"])
        self.assertIn("color: #123456", updated["style.css"])
        self.assertIn("background-color: #abcdef", updated["style.css"])
        self.assertIn("font-size: 32px", updated["style.css"])
        self.assertIn("margin-top: 12px", updated["style.css"])
        self.assertIn("padding-left: 16px", updated["style.css"])

    def test_rejects_text_edit_for_element_with_children(self) -> None:
        patch = WebsiteElementPatch(
            element_id="forge-div-1",
            changes=WebsiteElementChanges(text="替换容器"),
        )

        with self.assertRaisesRegex(BusinessException, "包含子元素"):
            apply_website_patches(self.files, [patch])


if __name__ == "__main__":
    unittest.main()
