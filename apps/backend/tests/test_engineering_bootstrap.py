"""Startup evidence comes from current source and remains bounded and versioned."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.generation.engineering.context import (
    INITIAL_CONTEXT_PATHS,
    MAX_INITIAL_CONTEXT_CHARS,
    MAX_INITIAL_FILE_CHARS,
    build_initial_source_context,
    initial_source_context,
)
from app.generation.workspace import create_workspace
from app.tools import files
from app.tools.paths import sha256_bytes


class EngineeringBootstrapTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = create_workspace(Path(directory.name) / "app")

    def test_one_pack_contains_current_entry_points_without_locks_or_secrets(self):
        body = "export const restoredRevision = true\n"
        (self.root / "frontend/src/App.tsx").write_text(body, encoding="utf-8")
        (self.root / ".env").write_text("SECRET=do-not-read", encoding="utf-8")
        pack = build_initial_source_context(self.root)
        by_path = {entry["path"]: entry for entry in pack["files"]}
        self.assertEqual(set(by_path), set(INITIAL_CONTEXT_PATHS))
        self.assertEqual(by_path["frontend/src/App.tsx"]["content"], body.rstrip())
        self.assertEqual(
            by_path["frontend/src/App.tsx"]["content_hash"],
            sha256_bytes((self.root / "frontend/src/App.tsx").read_bytes()),
        )
        self.assertTrue(all(not entry["content_truncated"] for entry in pack["files"]))
        self.assertEqual(pack["omitted_files"], [])

    def test_pack_bounds_long_files_and_marks_omitted_ranges(self):
        body = "line = 1\n" * 700
        for path in INITIAL_CONTEXT_PATHS:
            (self.root / path).write_text(body, encoding="utf-8")
        (self.root / "README.md").write_text("x" * 60000, encoding="utf-8")
        pack = build_initial_source_context(self.root)
        self.assertLessEqual(
            sum(len(entry["content"]) for entry in pack["files"]), MAX_INITIAL_CONTEXT_CHARS
        )
        for entry in pack["files"]:
            self.assertLessEqual(len(entry["content"]), MAX_INITIAL_FILE_CHARS)
            self.assertTrue(entry["content_truncated"])
            self.assertEqual(entry["end_line"], len(entry["content"].splitlines()))
            self.assertLess(entry["end_line"], entry["line_count"])
        self.assertIn({"path": "README.md", "reason": "file_too_large"}, pack["omitted_files"])
        self.assertTrue(any(row["reason"] == "context_budget" for row in pack["omitted_files"]))

    def test_recovery_never_replays_stale_source_and_new_evidence_takes_priority(self):
        checkpoint = {"initial_source_context": build_initial_source_context(self.root)}
        first = initial_source_context(checkpoint, self.root)
        path = "frontend/src/App.tsx"
        (self.root / path).write_text("new source", encoding="utf-8")
        changed = initial_source_context(checkpoint, self.root)
        self.assertIn(path, changed["stale_paths"])
        self.assertNotIn(path, {entry["path"] for entry in changed["files"]})
        self.assertIn(path, {entry["path"] for entry in first["files"]})
        current = changed["files"][0]
        projected = initial_source_context(checkpoint, self.root, source_evidence=[current])
        self.assertNotIn(current["path"], {entry["path"] for entry in projected["files"]})
        tail = {**current, "start_line": current["end_line"], "content": "last line only"}
        projected = initial_source_context(checkpoint, self.root, source_evidence=[tail])
        self.assertIn(current["path"], {entry["path"] for entry in projected["files"]})

    def test_file_index_prunes_dependency_and_platform_directories_before_descent(self):
        for name in ("node_modules", ".venv", "forgeai"):
            (self.root / name).mkdir(exist_ok=True)
            (self.root / name / "hidden.py").write_text("x", encoding="utf-8")
        nested = self.root / "frontend/src/forgeai"
        nested.mkdir()
        (nested / "local.ts").write_text("export const x = 1", encoding="utf-8")
        import os

        scan = os.scandir

        def guarded(path):
            self.assertNotIn(Path(path).name, {"node_modules", ".venv"})
            self.assertNotEqual(Path(path), self.root / "forgeai")
            return scan(path)

        with patch("app.tools.files.os.scandir", side_effect=guarded):
            listing = files.list_files(self.root)
        self.assertTrue(listing.ok)
        self.assertIn("backend/app/main.py", {entry["path"] for entry in listing.data["files"]})
        self.assertIn(
            "frontend/src/forgeai/local.ts", {entry["path"] for entry in listing.data["files"]}
        )
