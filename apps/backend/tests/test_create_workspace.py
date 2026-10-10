"""Tests for versioned template copying into engineering workspaces."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.generation import (
    DEFAULT_TEMPLATE_VERSION,
    FULLSTACK_REACT_V1,
    TemplateNotFoundError,
    WorkspaceExistsError,
    create_workspace,
    default_workspace_path,
    get_template_root,
    load_template_metadata,
)


class CreateWorkspaceTests(unittest.TestCase):
    def test_template_metadata_and_root(self) -> None:
        self.assertEqual(DEFAULT_TEMPLATE_VERSION, FULLSTACK_REACT_V1)
        meta = load_template_metadata()
        self.assertEqual(meta["template_version"], FULLSTACK_REACT_V1)
        self.assertEqual(meta["stack"]["frontend"], "react")
        root = get_template_root()
        self.assertTrue((root / "frontend" / "package.json").is_file())
        self.assertTrue((root / "frontend" / "package-lock.json").is_file())
        self.assertTrue((root / "frontend" / "src" / "App.tsx").is_file())
        self.assertTrue((root / "frontend" / "src" / "router.tsx").is_file())
        self.assertTrue((root / "frontend" / "public" / "assets" / "images").is_dir())
        self.assertTrue((root / "forgeai.smoke.json").is_file())
        self.assertTrue((root / "backend" / "app" / "main.py").is_file())
        self.assertTrue((root / "backend" / "uv.lock").is_file())
        self.assertTrue((root / "compose.yml").is_file())
        self.assertTrue(
            (root / "backend" / "alembic" / "versions" / "0001_create_app_meta.py").is_file()
        )

    def test_template_has_no_jobhub_business_modules(self) -> None:
        root = get_template_root(FULLSTACK_REACT_V1)
        forbidden_names = {"jobs.py", "applications.py", "job.py", "application.py", "resume.py"}
        found = {
            path.name
            for path in root.rglob("*")
            if path.is_file() and path.name.lower() in forbidden_names
        }
        self.assertEqual(found, set())
        text_blob = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in root.rglob("*")
            if path.is_file() and path.suffix in {".py", ".tsx", ".ts", ".md", ".json"}
        )
        self.assertNotIn("job_posting", text_blob.lower())
        self.assertNotIn("/api/v1/jobs", text_blob.lower())

    def test_create_workspace_copies_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "work" / "1" / "exec_a"
            workspace = create_workspace(dest)
            self.assertEqual(workspace, dest.resolve())
            self.assertTrue((workspace / "README.md").is_file())
            self.assertTrue((workspace / ".gitignore").is_file())
            self.assertTrue((workspace / "compose.yml").is_file())
            self.assertTrue((workspace / "frontend" / "src" / "App.tsx").is_file())
            self.assertTrue(
                (workspace / "frontend" / "public" / "assets" / "images" / "favicon.png").is_file()
            )
            self.assertTrue((workspace / "backend" / "app" / "api" / "health.py").is_file())
            self.assertTrue((workspace / "manifest.schema.json").is_file())
            smoke = json.loads((workspace / "forgeai.smoke.json").read_text(encoding="utf-8"))
            self.assertEqual(smoke["version"], 1)
            self.assertTrue(smoke["api_checks"])
            self.assertTrue(smoke["browser_checks"])

    def test_create_workspace_refuses_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "existing"
            dest.mkdir()
            with self.assertRaises(WorkspaceExistsError):
                create_workspace(dest)

    def test_unknown_template_version(self) -> None:
        with self.assertRaises(TemplateNotFoundError):
            get_template_root("does-not-exist")

    def test_default_workspace_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = default_workspace_path(tmp, 42, "exec_9")
            self.assertEqual(path, Path(tmp).resolve() / "work" / "42" / "exec_9")
            with self.assertRaises(ValueError):
                default_workspace_path(tmp, 1, "../escape")


if __name__ == "__main__":
    unittest.main()
