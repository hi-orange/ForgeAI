import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.agents.site_reviser import suggest_site_revise_reply
from app.schemas.project import (
    ProjectElementAiEdit,
    ProjectWebsiteRevise,
    ProjectWebsiteReviseFocus,
)
from app.services import project as project_service


class SiteReviserTests(unittest.TestCase):
    def setUp(self) -> None:
        html = (
            "<!DOCTYPE html><html><head><title>Demo</title></head>"
            '<body><header><h1 data-forge-id="forge-h1-1">Hello World Site</h1></header>'
            '<div data-forge-id="forge-div-1">简约电商</div>'
            "<main><p>Welcome to the demo landing page content area.</p></main>"
            "</body></html>"
        )
        css = "body{margin:0;font-family:sans-serif}h1{font-size:32px;color:#111}"
        self.current_files = json.dumps(
            {"index.html": html, "style.css": css, "script.js": ""},
            ensure_ascii=False,
        )
        self.payload = ProjectWebsiteRevise(
            instruction="把整站改成深色主题，并更新首页文案",
            base_revision=1,
        )

    @patch("app.agents.site_reviser.chat_completion")
    def test_returns_applied_for_clear_instruction(self, completion) -> None:
        updated_html = (
            "<!DOCTYPE html><html><head><title>Dark</title></head>"
            "<body><header><h1>Dark Site Title Here</h1></header>"
            "<main><p>Updated landing copy for the whole website revise path.</p></main>"
            "</body></html>"
        )
        updated_css = (
            "body{background:#111;color:#fff;margin:0;font-family:sans-serif}h1{font-size:32px}"
        )
        completion.return_value = json.dumps(
            {
                "mode": "files",
                "message": "已改为深色主题并更新首页文案。",
                "files": {
                    "index.html": updated_html,
                    "style.css": updated_css,
                    "script.js": "",
                },
            }
        )

        reply = suggest_site_revise_reply(
            self.payload,
            current_files=self.current_files,
            approved_spec=None,
        )

        self.assertEqual(reply.mode, "applied")
        self.assertIsNotNone(reply.files_json)
        self.assertIn("Dark Site", reply.files_json or "")

    @patch("app.agents.site_reviser.chat_completion")
    def test_clarifies_ambiguous_instruction(self, completion) -> None:
        completion.return_value = json.dumps(
            {
                "mode": "message",
                "message": "你想改配色、文案，还是页面结构？",
            }
        )
        payload = self.payload.model_copy(update={"instruction": "改一下"})

        reply = suggest_site_revise_reply(
            payload,
            current_files=self.current_files,
            approved_spec=None,
        )

        self.assertEqual(reply.mode, "message")
        self.assertIsNone(reply.files_json)

    @patch("app.agents.site_reviser.chat_completion")
    def test_noop_files_become_message(self, completion) -> None:
        completion.return_value = json.dumps(
            {
                "mode": "files",
                "message": "已更新",
                "files": json.loads(self.current_files),
            }
        )

        reply = suggest_site_revise_reply(
            self.payload,
            current_files=self.current_files,
            approved_spec=None,
        )

        self.assertEqual(reply.mode, "message")
        self.assertIsNone(reply.files_json)

    @patch("app.agents.site_reviser.chat_completion")
    def test_focus_clear_text_rewrite_applies_without_llm(self, completion) -> None:
        payload = ProjectWebsiteRevise(
            instruction="改成电商平台",
            base_revision=1,
            focus=ProjectWebsiteReviseFocus(
                element_id="forge-div-1",
                tag_name="div",
                text="简约电商",
                text_editable=True,
                styles={"color": "#111111", "font-size": "24px"},
            ),
        )

        reply = suggest_site_revise_reply(
            payload,
            current_files=self.current_files,
            approved_spec=None,
        )

        self.assertEqual(reply.mode, "applied")
        self.assertIsNotNone(reply.files_json)
        self.assertIn("电商平台", reply.files_json or "")
        self.assertNotIn("简约电商", reply.files_json or "")
        self.assertIn("电商平台", reply.message)
        completion.assert_not_called()

    @patch("app.agents.site_reviser.chat_completion")
    def test_focus_style_change_uses_patch_llm(self, completion) -> None:
        completion.return_value = json.dumps(
            {
                "mode": "patch",
                "message": "好，字号调大了。",
                "text": None,
                "styles": {"font-size": "36px"},
            }
        )
        payload = ProjectWebsiteRevise(
            instruction="字号再大一点",
            base_revision=1,
            focus=ProjectWebsiteReviseFocus(
                element_id="forge-h1-1",
                tag_name="h1",
                text="Hello World Site",
                text_editable=True,
                styles={"color": "#111111", "font-size": "32px"},
            ),
        )

        reply = suggest_site_revise_reply(
            payload,
            current_files=self.current_files,
            approved_spec=None,
        )

        self.assertEqual(reply.mode, "applied")
        files = json.loads(reply.files_json or "{}")
        self.assertIn('[data-forge-id="forge-h1-1"]', files["style.css"])
        self.assertIn("font-size: 36px", files["style.css"])
        user_content = completion.call_args.kwargs["messages"][-1]["content"]
        self.assertIn("forge-h1-1", user_content)
        self.assertIn("字号再大一点", user_content)

    @patch("app.services.project.deterministic_issues", return_value=[])
    @patch("app.agents.site_reviser.suggest_site_revise_reply")
    @patch("app.services.project.get_user_project")
    def test_service_saves_revision(
        self,
        get_project,
        suggest_reply,
        _issues,
    ) -> None:
        from app.schemas.project import ProjectWebsiteReviseReply

        project = SimpleNamespace(
            id=1,
            user_id=1,
            name="Demo",
            description=None,
            prompt="demo",
            prd=None,
            approved_spec=None,
            approved_at=None,
            generated_files=self.current_files,
            build_error=None,
            validation_report=None,
            website_revision=1,
            built_at=None,
            status="completed",
            created_at=datetime(2026, 1, 1),
            updated_at=datetime(2026, 1, 1),
        )
        get_project.return_value = project
        suggest_reply.return_value = ProjectWebsiteReviseReply(
            mode="applied",
            message="已整站更新",
            files_json=json.dumps(
                {
                    "index.html": (
                        "<!DOCTYPE html><html><head><title>New</title></head>"
                        "<body><h1>New Site Title Content Area</h1>"
                        "<p>Revised whole website content for save path.</p></body></html>"
                    ),
                    "style.css": "body{margin:0;background:#0a0a0a;color:#f5f5f5}",
                    "script.js": "",
                }
            ),
        )

        db = MagicMock()
        user = MagicMock()
        reply = project_service.revise_project_website(db, user, 1, self.payload)

        self.assertEqual(reply.mode, "applied")
        self.assertEqual(project.website_revision, 2)
        db.commit.assert_called_once()

    @patch("app.services.project.revise_project_website")
    def test_element_endpoint_maps_to_revise(self, revise) -> None:
        from app.schemas.project import ProjectWebsiteReviseReply

        revise.return_value = ProjectWebsiteReviseReply(
            mode="applied",
            message="好，已改成电商网站。",
        )
        payload = ProjectElementAiEdit(
            element_id="forge-h1-1",
            tag_name="h1",
            text="原始标题",
            text_editable=True,
            styles={"color": "#111111"},
            instruction="改成电商网站",
            base_revision=1,
        )

        reply = project_service.suggest_project_element_edit(MagicMock(), MagicMock(), 1, payload)

        self.assertEqual(reply.mode, "applied")
        self.assertEqual(reply.message, "好，已改成电商网站。")
        revise_payload = revise.call_args.args[3]
        self.assertEqual(revise_payload.focus.element_id, "forge-h1-1")
        self.assertEqual(revise_payload.instruction, "改成电商网站")


if __name__ == "__main__":
    unittest.main()
