"""Workspace tool path safety and hash-checked writes."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.exceptions import ConflictException
from app.tools.files import (
    apply_patch,
    edit_file_by_replace,
    list_files,
    read_file,
    search_code,
)
from app.tools.paths import sha256_bytes


class WorkspaceFileToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="forgeai-tools-")))
        (self.root / "backend" / "app").mkdir(parents=True)
        (self.root / "backend" / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    def test_list_and_search_and_read(self):
        listed = list_files(self.root)
        self.assertTrue(listed.ok)
        self.assertEqual(listed.data["files"][0]["path"], "backend/app/main.py")
        found = search_code(self.root, query="print")
        self.assertEqual(found.data["hits"][0]["path"], "backend/app/main.py")
        read = read_file(self.root, path="backend/app/main.py")
        self.assertIn("print", read.data["content"])
        self.assertEqual(
            read.data["content_hash"],
            sha256_bytes((self.root / "backend" / "app" / "main.py").read_bytes()),
        )

    def test_rejects_path_escape(self):
        with self.assertRaises(ConflictException):
            read_file(self.root, path="../secret.py")

    def test_patch_requires_matching_hash(self):
        current = sha256_bytes((self.root / "backend" / "app" / "main.py").read_bytes())
        conflict = apply_patch(
            self.root,
            path="backend/app/main.py",
            content="print('new')\n",
            expected_hash="0" * 64,
        )
        self.assertFalse(conflict.ok)
        self.assertEqual(conflict.error_code, "HASH_CONFLICT")
        ok = apply_patch(
            self.root,
            path="backend/app/main.py",
            content="print('new')\n",
            expected_hash=current,
        )
        self.assertTrue(ok.ok)
        self.assertEqual(
            (self.root / "backend" / "app" / "main.py").read_text(encoding="utf-8"),
            "print('new')\n",
        )

    def test_large_file_requires_bounded_range(self):
        target = self.root / "backend" / "app" / "large.py"
        target.write_text(
            "".join(f"value_{index} = {index}\n" for index in range(1200)),
            encoding="utf-8",
        )

        full = read_file(self.root, path="backend/app/large.py")
        self.assertFalse(full.ok)
        self.assertEqual(full.error_code, "FILE_TOO_LARGE")
        self.assertEqual(full.data["line_count"], 1200)

        excerpt = read_file(
            self.root,
            path="backend/app/large.py",
            start_line=500,
            end_line=650,
        )
        self.assertTrue(excerpt.ok)
        self.assertIn("value_500", excerpt.data["content"])

        oversized = read_file(
            self.root,
            path="backend/app/large.py",
            start_line=1,
            end_line=500,
        )
        self.assertFalse(oversized.ok)
        self.assertEqual(oversized.error_code, "RANGE_TOO_LARGE")

    def test_existing_file_write_requires_observed_hash(self):
        target = self.root / "backend" / "app" / "main.py"
        original = target.read_text(encoding="utf-8")
        result = apply_patch(
            self.root,
            path="backend/app/main.py",
            content="print('unsafe overwrite')\n",
            expected_hash=None,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "READ_REQUIRED")
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_local_edit_requires_one_exact_match_and_hash(self):
        target = self.root / "backend" / "app" / "main.py"
        current_hash = sha256_bytes(target.read_bytes())

        result = edit_file_by_replace(
            self.root,
            path="backend/app/main.py",
            old_text="print('ok')",
            new_text="print('changed')",
            expected_hash=current_hash,
        )

        self.assertTrue(result.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "print('changed')\n")
