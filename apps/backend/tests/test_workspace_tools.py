"""Workspace tool path safety and hash-checked writes."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.exceptions import ConflictException
from app.tools.files import apply_patch, list_files, read_file, search_code
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
