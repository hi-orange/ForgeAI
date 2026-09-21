"""Current-workspace lexical RAG returns bounded, fresh, attributable excerpts."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.retrieval.code_context import retrieve_code_context
from app.tools.paths import sha256_bytes


class CodeRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="forgeai-rag-")))
        (self.root / "backend" / "app").mkdir(parents=True)

    def _write(self, name: str, content: str) -> Path:
        target = self.root / "backend" / "app" / name
        target.write_text(content, encoding="utf-8")
        return target

    def test_twenty_code_location_queries_return_expected_source(self):
        cases = [
            ("authentication session token", "auth_service.py"),
            ("invoice tax total", "invoice_service.py"),
            ("booking availability calendar", "booking_service.py"),
            ("inventory reorder threshold", "inventory_service.py"),
            ("profile avatar upload", "profile_service.py"),
            ("article publish slug", "article_service.py"),
            ("comment moderation queue", "comment_service.py"),
            ("notification unread badge", "notification_service.py"),
            ("order checkout address", "order_service.py"),
            ("catalog category filter", "catalog_service.py"),
            ("resume education history", "resume_service.py"),
            ("job application status", "application_service.py"),
            ("survey response option", "survey_service.py"),
            ("event attendee capacity", "event_service.py"),
            ("support ticket priority", "support_service.py"),
            ("expense receipt amount", "expense_service.py"),
            ("habit streak reminder", "habit_service.py"),
            ("course lesson progress", "course_service.py"),
            ("library loan due", "library_service.py"),
            ("workspace member role", "workspace_service.py"),
        ]
        for query, filename in cases:
            self._write(filename, f'"""Handles {query} workflows."""\n')

        for query, filename in cases:
            with self.subTest(query=query):
                result = retrieve_code_context(self.root, query=query, max_results=3)
                self.assertTrue(result.ok)
                self.assertTrue(result.data["hits"])
                self.assertEqual(result.data["hits"][0]["path"], f"backend/app/{filename}")
                self.assertEqual(result.data["hits"][0]["retrieval_method"], "lexical_tfidf")
                self.assertEqual(result.data["hits"][0]["project_scope"], "current_workspace")

    def test_retrieval_reads_current_file_and_does_not_use_stale_hits(self):
        target = self._write("shipping.py", "def legacy_invoice_total():\n    return 0\n")
        before = retrieve_code_context(self.root, query="legacy invoice total")
        self.assertEqual(before.data["hits"][0]["path"], "backend/app/shipping.py")

        target.write_text(
            "def shipment_tracking_status():\n    return 'ready'\n",
            encoding="utf-8",
        )
        after = retrieve_code_context(self.root, query="shipment tracking status")

        self.assertEqual(after.data["hits"][0]["path"], "backend/app/shipping.py")
        self.assertIn("shipment_tracking_status", after.data["hits"][0]["content"])
        self.assertNotEqual(
            before.data["hits"][0]["content_hash"],
            after.data["hits"][0]["content_hash"],
        )
        self.assertEqual(after.data["hits"][0]["content_hash"], sha256_bytes(target.read_bytes()))

    def test_retrieval_excludes_non_source_and_skipped_directories(self):
        self._write("public.py", "SAFE_PUBLIC_MARKER = True\n")
        (self.root / ".env").write_text("SECRET_PRIVATE_MARKER=value\n", encoding="utf-8")
        (self.root / "data").mkdir()
        (self.root / "data" / "private.py").write_text(
            "SECRET_PRIVATE_MARKER = True\n", encoding="utf-8"
        )

        result = retrieve_code_context(self.root, query="SECRET_PRIVATE_MARKER")

        self.assertTrue(result.ok)
        self.assertEqual(result.data["hits"], [])

    def test_retrieval_never_reads_a_different_workspace(self):
        foreign_root = Path(self.enterContext(TemporaryDirectory(prefix="forgeai-rag-foreign-")))
        (foreign_root / "backend" / "app").mkdir(parents=True)
        (foreign_root / "backend" / "app" / "private.py").write_text(
            "CROSS_WORKSPACE_PRIVATE_MARKER = True\n",
            encoding="utf-8",
        )

        result = retrieve_code_context(
            self.root,
            query="CROSS_WORKSPACE_PRIVATE_MARKER",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["hits"], [])


if __name__ == "__main__":
    unittest.main()
