"""Tests for versioned template copying into engineering workspaces."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
from app.generation.workspace import prepare_engineering_workspace, publish_workspace_directory


class CreateWorkspaceTests(unittest.TestCase):
    def test_workspace_publication_retries_windows_contention_then_renames(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, dest = Path(tmp) / "staging", Path(tmp) / "published"
            source.mkdir()
            (source / "content").write_text("preserve me", encoding="utf-8")
            failure = PermissionError("held handle")
            failure.winerror = 5
            original = Path.rename
            attempts = iter([failure, None])

            def rename(path, target):
                error = next(attempts)
                if error is not None:
                    raise error
                return original(path, target)

            with (
                patch.object(Path, "rename", autospec=True, side_effect=rename),
                patch("app.generation.workspace.time.sleep") as sleep,
            ):
                publish_workspace_directory(source, dest)
            sleep.assert_called_once_with(0.05)
            self.assertEqual((dest / "content").read_text(encoding="utf-8"), "preserve me")
            self.assertFalse(source.exists())

    def test_workspace_publication_never_overwrites_or_hides_persistent_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, dest = Path(tmp) / "staging", Path(tmp) / "published"
            source.mkdir()
            failure = PermissionError("denied")
            failure.winerror = 5
            for exists, attempts in ((False, 5), (True, 1)):
                if exists:
                    dest.mkdir()
                with (
                    patch.object(Path, "rename", side_effect=failure) as rename,
                    patch("app.generation.workspace.time.sleep"),
                    self.assertRaises(PermissionError) as raised,
                ):
                    publish_workspace_directory(source, dest)
                self.assertIs(raised.exception, failure)
                self.assertEqual(rename.call_count, attempts)
                self.assertTrue(source.exists())

    def test_prepare_preserves_filesystem_error_when_no_concurrent_destination_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            denied = PermissionError("directory is held by another process")
            with patch.object(Path, "rename", side_effect=denied):
                with self.assertRaises(PermissionError) as raised:
                    prepare_engineering_workspace(
                        1,
                        "run_test",
                        task_id="task_test",
                        approved_item_id="ci_test",
                        runtime_data_root=tmp,
                    )
            self.assertIs(raised.exception, denied)
            self.assertFalse(default_workspace_path(tmp, 1, "run_test").exists())

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
