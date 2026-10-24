"""Contracts for the trusted generated-app end-to-end smoke manifest."""

from __future__ import annotations

import base64
import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sandbox.smoke_manifest import (
    REQUIRED_THEME_TOKENS,
    SMOKE_CONTRACT,
    all_check_timeout_seconds,
    browser_smoke_timeout_seconds,
    validate_smoke_manifest,
)

CHECK_SCRIPT = Path(__file__).resolve().parents[1] / "sandbox" / "check.py"
SPEC = importlib.util.spec_from_file_location("forgeai_sandbox_check", CHECK_SCRIPT)
assert SPEC and SPEC.loader
sandbox_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sandbox_check)


class SmokeManifestTests(unittest.TestCase):
    def test_browser_contract_describes_required_check_fields(self) -> None:
        spec = SMOKE_CONTRACT["browser"]
        self.assertEqual(spec["required_fields"], ["id", "route"])
        self.assertEqual(spec["route"]["type"], "local_path")
        self.assertTrue(spec["route"]["example"].startswith("/"))

    def test_visual_contract_description_matches_validator(self) -> None:
        spec = SMOKE_CONTRACT["visual_contract"]
        self.assertEqual(spec["applies_to_template"], "fullstack-react-v1")
        self.assertEqual(spec["theme_file"], {"type": "source_path", "prefix": "frontend/"})
        self.assertEqual(set(spec["theme_mode"]["enum"]), {"template", "custom", "preserve"})
        self.assertEqual(spec["theme_reason"]["min_trimmed_length"], 8)
        self.assertEqual(set(spec["required_tokens"]["must_include"]), REQUIRED_THEME_TOKENS)
        self.assertEqual(spec["required_tokens"]["item_prefix"], "--")
        self.assertEqual(spec["local_assets"]["max_items"], 30)
        self.assertTrue(spec["local_assets"]["optional"])

    def test_browser_timeout_scales_with_manifest_and_remains_bounded(self) -> None:
        self.assertEqual(browser_smoke_timeout_seconds({"browser_checks": [{"id": "one"}]}), 120)
        checks = [
            {
                "id": f"case-{index}",
                "actions": [{"type": "expect_no_horizontal_scroll"}] * (12 if index < 9 else 11),
            }
            for index in range(19)
        ]
        manifest = {"browser_checks": checks}
        self.assertEqual(sum(len(check["actions"]) for check in checks), 218)
        self.assertEqual(browser_smoke_timeout_seconds(manifest), 826)
        self.assertEqual(all_check_timeout_seconds(manifest, 1200), 1006)
        self.assertEqual(all_check_timeout_seconds(manifest, 300), 300)
        saturated = {
            "browser_checks": [{"id": f"case-{index}", "actions": [{}] * 8} for index in range(50)]
        }
        self.assertEqual(browser_smoke_timeout_seconds(saturated), 900)
        self.assertEqual(all_check_timeout_seconds(saturated, 1200), 1080)

    def test_browser_runner_uses_manifest_budget_and_preserves_rendered_checks(self) -> None:
        manifest = self.baseline()
        manifest["browser_checks"] = [
            {**manifest["browser_checks"][0], "id": f"case-{index}"} for index in range(20)
        ]
        with TemporaryDirectory() as tmp:
            rendered_path = Path(tmp) / "rendered.json"
            with (
                patch.object(sandbox_check, "Path", return_value=rendered_path),
                patch.object(sandbox_check, "command") as command,
            ):
                sandbox_check.run_browser_smoke(manifest, {"item_id": 42}, "http://127.0.0.1:5173")
            self.assertEqual(command.call_args.kwargs["timeout"], 485)
            rendered = json.loads(rendered_path.read_text(encoding="utf-8"))
            self.assertEqual(len(rendered["browser_checks"]), 20)

    def test_materialize_restores_public_binary_image(self):
        image = b"\xff\xd8\xffgenerated"
        with (
            TemporaryDirectory() as directory,
            patch.object(sandbox_check, "ROOT", Path(directory)),
        ):
            sandbox_check.materialize(
                {
                    "frontend/public/images/hero.jpg": {
                        "encoding": "base64",
                        "content": base64.b64encode(image).decode("ascii"),
                    }
                }
            )

            self.assertEqual(
                (Path(directory) / "frontend/public/images/hero.jpg").read_bytes(),
                image,
            )

    def test_materialize_rejects_binary_outside_public_directory(self):
        with (
            TemporaryDirectory() as directory,
            patch.object(sandbox_check, "ROOT", Path(directory)),
            self.assertRaisesRegex(ValueError, "Invalid binary source"),
        ):
            sandbox_check.materialize(
                {
                    "backend/private.jpg": {
                        "encoding": "base64",
                        "content": base64.b64encode(b"private").decode("ascii"),
                    }
                }
            )

    def write_manifest(self, root: Path, manifest: dict) -> Path:
        template = root / "template.json"
        if not template.exists():
            template.write_text('{"template_version":"test-fixture"}', encoding="utf-8")
        path = root / "forgeai.smoke.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def baseline(self) -> dict:
        return {
            "version": 1,
            "visual_contract": {
                "theme_file": "frontend/src/index.css",
                "theme_mode": "custom",
                "theme_reason": "为商品浏览体验使用新的领域配色",
                "required_tokens": [
                    "--color-primary",
                    "--color-accent",
                    "--color-muted",
                ],
                "local_assets": ["frontend/src/assets/hero.svg"],
            },
            "api_checks": [
                {
                    "id": "create",
                    "method": "POST",
                    "path": "/api/v1/items",
                    "json": {"name": "one"},
                    "expect_status": 201,
                    "expect_json_paths": ["data.id"],
                    "capture": {"item_id": "data.id"},
                },
                {
                    "id": "read",
                    "path": "/api/v1/items/${item_id}",
                    "expect_json_paths": ["data.name"],
                },
            ],
            "browser_checks": [
                {
                    "id": "items",
                    "route": "/items",
                    "expect_text": ["Items"],
                    "expect_api": ["/api/v1/items"],
                    "actions": [
                        {"type": "click", "selector": "[data-testid='create']"},
                        {"type": "expect_path", "path": "/items"},
                    ],
                }
            ],
        }

    def test_accepts_cross_layer_business_journey(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "frontend/src/assets").mkdir(parents=True)
            (root / "frontend/src/index.css").write_text(
                ":root { --color-primary: #123; --color-accent: #456; --color-muted: #789; }",
                encoding="utf-8",
            )
            (root / "frontend/src/assets/hero.svg").write_text("<svg/>", encoding="utf-8")
            (root / "template.json").write_text(
                '{"template_version":"fullstack-react-v1"}', encoding="utf-8"
            )
            template_theme = root / "template-index.css"
            template_theme.write_text(
                ":root { --color-primary: #111; --color-accent: #222; --color-muted: #333; }",
                encoding="utf-8",
            )
            path = self.write_manifest(root, self.baseline())
            with (
                patch.object(sandbox_check, "ROOT", root),
                patch.object(sandbox_check, "SMOKE_MANIFEST", path),
                patch.object(sandbox_check, "TEMPLATE_THEME_FILE", template_theme),
            ):
                manifest = sandbox_check.load_smoke_manifest()
                sandbox_check.validate_visual_contract(manifest)
        self.assertEqual(len(manifest["api_checks"]), 2)
        rendered = sandbox_check._render_variables(manifest, {"item_id": 42})
        self.assertEqual(rendered["api_checks"][1]["path"], "/api/v1/items/42")

    def test_json_path_supports_root_and_array_notation(self) -> None:
        document = [{"name": "手冲咖啡", "details": {"price": 32}}]
        self.assertEqual(sandbox_check._json_path(document, "$[0].name"), "手冲咖啡")
        self.assertEqual(sandbox_check._json_path(document, "[0].details.price"), 32)
        self.assertEqual(sandbox_check._json_path({"data": document}, "$.data[0].name"), "手冲咖啡")
        self.assertEqual(sandbox_check._json_path({"data": {"id": 7}}, "data.id"), 7)

    def test_captured_login_token_reaches_later_authorization_header(self) -> None:
        manifest = self.baseline()
        manifest.pop("visual_contract")
        manifest["api_checks"] = [
            {
                "id": "login",
                "method": "POST",
                "path": "/api/v1/auth/login",
                "json": {"username": "seeker", "password": "password"},
                "capture": {"seeker_token": "data.access_token"},
            },
            {
                "id": "profile",
                "path": "/api/v1/profile",
                "headers": {"Authorization": "Bearer ${seeker_token}"},
            },
        ]
        with TemporaryDirectory() as tmp:
            validate_smoke_manifest(manifest, root=Path(tmp), template_metadata={})

        class Response:
            def __init__(self, body):
                self.status = 200
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return self.body

        requests = []

        def respond(request, *, timeout):
            requests.append(request)
            assert timeout == 10
            body = b'{"data":{"access_token":"real-token"}}' if len(requests) == 1 else b"{}"
            return Response(body)

        with patch.object(sandbox_check.urllib.request, "urlopen", side_effect=respond):
            variables = sandbox_check.run_api_smoke(manifest, "http://127.0.0.1:8000")

        self.assertEqual(variables["seeker_token"], "real-token")
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer real-token")

    def test_api_check_rejects_unknown_auth_field_with_supported_flow(self) -> None:
        manifest = self.baseline()
        manifest.pop("visual_contract")
        manifest["api_checks"][1]["auth"] = "seeker"
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as error:
                validate_smoke_manifest(manifest, root=Path(tmp), template_metadata={})
        message = str(error.exception)
        for fragment in ("api_checks[read]", "auth", "capture", "headers", "${seeker_token}"):
            self.assertIn(fragment, message)

    def test_rejects_external_api_and_browser_routes(self) -> None:
        manifest = self.baseline()
        manifest.pop("visual_contract")
        manifest["api_checks"][0]["path"] = "https://example.com/items"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_manifest(root, manifest)
            with (
                patch.object(sandbox_check, "ROOT", root),
                patch.object(sandbox_check, "SMOKE_MANIFEST", path),
                self.assertRaisesRegex(ValueError, "站内绝对路径"),
            ):
                sandbox_check.load_smoke_manifest()

    def test_missing_browser_routes_identify_all_checks_and_required_field(self) -> None:
        manifest = self.baseline()
        manifest.pop("visual_contract")
        manifest["browser_checks"] = [
            {**manifest["browser_checks"][0], "id": "home", "route": "/"},
            {**manifest["browser_checks"][0], "id": "jobs", "route": "/jobs"},
        ]
        for check in manifest["browser_checks"]:
            del check["route"]
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as error:
                validate_smoke_manifest(manifest, root=Path(tmp), template_metadata={})
        self.assertIn("browser_checks[0][home].route", str(error.exception))
        self.assertIn("browser_checks[1][jobs].route", str(error.exception))
        self.assertIn("缺少必填字段 route", str(error.exception))

    def test_invalid_browser_route_identifies_check_and_field(self) -> None:
        manifest = self.baseline()
        manifest.pop("visual_contract")
        manifest["browser_checks"][0]["route"] = "https://example.com/jobs"
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as error:
                validate_smoke_manifest(manifest, root=Path(tmp), template_metadata={})
        self.assertIn("browser_checks[0][items].route", str(error.exception))
        self.assertIn("站内绝对路径", str(error.exception))

    def test_browser_check_must_prove_a_frontend_api_request(self) -> None:
        manifest = self.baseline()
        manifest.pop("visual_contract")
        manifest["browser_checks"][0]["expect_api"] = []
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_manifest(root, manifest)
            with (
                patch.object(sandbox_check, "ROOT", root),
                patch.object(sandbox_check, "SMOKE_MANIFEST", path),
                self.assertRaisesRegex(ValueError, "必须声明 expect_api"),
            ):
                sandbox_check.load_smoke_manifest()

    def test_actions_require_their_runtime_fields(self) -> None:
        manifest = self.baseline()
        manifest.pop("visual_contract")
        manifest["browser_checks"][0]["actions"] = [{"type": "fill", "selector": "#name"}]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_manifest(root, manifest)
            with (
                patch.object(sandbox_check, "ROOT", root),
                patch.object(sandbox_check, "SMOKE_MANIFEST", path),
                self.assertRaisesRegex(ValueError, "缺少字段"),
            ):
                sandbox_check.load_smoke_manifest()

    def test_extended_safe_browser_actions_and_viewport_are_accepted(self) -> None:
        manifest = self.baseline()
        manifest.pop("visual_contract")
        manifest["browser_checks"][0]["viewport"] = {"width": 375, "height": 812}
        manifest["browser_checks"][0]["actions"] = [
            {"type": "navigate", "url": "/items"},
            {"type": "wait_for_selector", "selector": "#items"},
            {"type": "expect_any_text", "texts": ["营业中", "已打烊"]},
            {"type": "expect_count", "selector": "#items li", "count": 4},
            {"type": "expect_no_horizontal_scroll"},
            {"type": "screenshot", "name": "items-mobile"},
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_manifest(root, manifest)
            with (
                patch.object(sandbox_check, "ROOT", root),
                patch.object(sandbox_check, "SMOKE_MANIFEST", path),
            ):
                loaded = sandbox_check.load_smoke_manifest()

        self.assertEqual(loaded["browser_checks"][0]["viewport"]["width"], 375)

    def test_navigate_rejects_external_url_and_count_requires_integer(self) -> None:
        manifest = self.baseline()
        manifest.pop("visual_contract")
        manifest["browser_checks"][0]["actions"] = [
            {"type": "navigate", "url": "https://example.com"}
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_manifest(root, manifest)
            with (
                patch.object(sandbox_check, "ROOT", root),
                patch.object(sandbox_check, "SMOKE_MANIFEST", path),
                self.assertRaisesRegex(ValueError, "站内绝对路径"),
            ):
                sandbox_check.load_smoke_manifest()

        manifest["browser_checks"][0]["actions"] = [
            {"type": "expect_count", "selector": "#items li", "count": "4"}
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_manifest(root, manifest)
            with (
                patch.object(sandbox_check, "ROOT", root),
                patch.object(sandbox_check, "SMOKE_MANIFEST", path),
                self.assertRaisesRegex(ValueError, "count 必须是整数"),
            ):
                sandbox_check.load_smoke_manifest()

    def test_contract_rejects_unapproved_top_level_commands(self) -> None:
        manifest = self.baseline()
        manifest.pop("visual_contract")
        manifest["seed_checks"] = [{"command": "python -m app.db.seed"}]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_manifest(root, manifest)
            with (
                patch.object(sandbox_check, "ROOT", root),
                patch.object(sandbox_check, "SMOKE_MANIFEST", path),
                self.assertRaisesRegex(ValueError, "未授权顶层字段：seed_checks"),
            ):
                sandbox_check.load_smoke_manifest()

    def test_business_app_cannot_keep_template_palette(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "frontend/src/assets").mkdir(parents=True)
            theme = ":root { --color-primary: #111; --color-accent: #222; --color-muted: #333; }"
            (root / "frontend/src/index.css").write_text(theme, encoding="utf-8")
            (root / "frontend/src/assets/hero.svg").write_text("<svg/>", encoding="utf-8")
            (root / "template.json").write_text(
                '{"template_version":"fullstack-react-v1"}', encoding="utf-8"
            )
            template_theme = root / "template-index.css"
            template_theme.write_text(theme, encoding="utf-8")
            path = self.write_manifest(root, self.baseline())
            with (
                patch.object(sandbox_check, "ROOT", root),
                patch.object(sandbox_check, "SMOKE_MANIFEST", path),
                patch.object(sandbox_check, "TEMPLATE_THEME_FILE", template_theme),
            ):
                manifest = sandbox_check.load_smoke_manifest()
                with self.assertRaisesRegex(ValueError, "模板默认配色"):
                    sandbox_check.validate_visual_contract(manifest)

    def test_approved_palette_can_be_preserved_across_feature_iterations(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "frontend/src/assets").mkdir(parents=True)
            theme = ":root { --color-primary: #111; --color-accent: #222; --color-muted: #333; }"
            (root / "frontend/src/index.css").write_text(theme, encoding="utf-8")
            (root / "frontend/src/assets/hero.svg").write_text("<svg/>", encoding="utf-8")
            (root / "template.json").write_text(
                '{"template_version":"fullstack-react-v1"}', encoding="utf-8"
            )
            template_theme = root / "template-index.css"
            template_theme.write_text(theme, encoding="utf-8")
            manifest_data = self.baseline()
            manifest_data["visual_contract"]["theme_mode"] = "preserve"
            manifest_data["visual_contract"]["theme_reason"] = "用户明确要求继续使用当前已批准配色"
            path = self.write_manifest(root, manifest_data)
            with (
                patch.object(sandbox_check, "ROOT", root),
                patch.object(sandbox_check, "SMOKE_MANIFEST", path),
                patch.object(sandbox_check, "TEMPLATE_THEME_FILE", template_theme),
            ):
                manifest = sandbox_check.load_smoke_manifest()
                sandbox_check.validate_visual_contract(manifest)


if __name__ == "__main__":
    unittest.main()
