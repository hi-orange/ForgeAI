"""Contracts for the trusted generated-app end-to-end smoke manifest."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

CHECK_SCRIPT = Path(__file__).resolve().parents[1] / "sandbox" / "check.py"
SPEC = importlib.util.spec_from_file_location("forgeai_sandbox_check", CHECK_SCRIPT)
assert SPEC and SPEC.loader
sandbox_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sandbox_check)


class SmokeManifestTests(unittest.TestCase):
    def write_manifest(self, root: Path, manifest: dict) -> Path:
        template = root / "template.json"
        if not template.exists():
            template.write_text('{"template_version":"fullstack-v1"}', encoding="utf-8")
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
                self.assertRaisesRegex(ValueError, "缺少字符串字段"),
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
