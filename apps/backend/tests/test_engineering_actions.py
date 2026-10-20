"""The engineering tool boundary never writes live source before a fenced commit."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.generation.engineering.actions import PreparedAction, commit_action, prepare_action
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.tools.files import restore_workspace_revision
from app.tools.paths import sha256_bytes


class EngineeringActionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="forgeai-actions-")))

    def source(self, path, content=b"VALUE = 1\n"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return sha256_bytes(content)

    def test_write_is_staged_until_commit_and_revision_restores_original(self):
        path = "backend/app/module.py"
        digest = self.source(path)
        call = ToolCall(
            id="write",
            name="write_file",
            arguments={
                "path": path,
                "content": "VALUE = 2\n",
                "expected_hash": digest,
            },
        )
        action = prepare_action(call, self.root, {path: digest})
        self.assertTrue(action.result.ok, action.result.summary)
        self.assertEqual((self.root / path).read_bytes(), b"VALUE = 1\n")
        result = commit_action(action, self.root, revision_id="execution")
        self.assertTrue(result.ok)
        self.assertEqual((self.root / path).read_bytes(), b"VALUE = 2\n")
        restore_workspace_revision(self.root, revision_id="execution")
        self.assertEqual((self.root / path).read_bytes(), b"VALUE = 1\n")

    def test_guessed_hash_without_read_evidence_is_rejected(self):
        digest = self.source("module.py")
        action = prepare_action(
            ToolCall(
                id="write",
                name="write_file",
                arguments={
                    "path": "module.py",
                    "content": "VALUE = 2",
                    "expected_hash": digest,
                },
            ),
            self.root,
            {},
        )
        self.assertFalse(action.result.ok)
        self.assertEqual(action.updates, {})

    def test_unified_write_supports_single_and_multiple_files(self):
        for count in (1, 2, 8):
            with self.subTest(count=count):
                writes = [
                    {"path": f"batch_{count}/{i}.py", "content": f"VALUE = {i}\n"}
                    for i in range(count)
                ]
                action = prepare_action(
                    ToolCall(id="write", name="write_files", arguments={"writes": writes}),
                    self.root,
                    {},
                )
                self.assertTrue(action.result.ok, action.result.summary)
                for row in writes:
                    self.assertFalse((self.root / row["path"]).exists())
                result = commit_action(action, self.root, revision_id=f"write-{count}")
                self.assertTrue(result.ok, result.summary)
                self.assertEqual(len(result.data["files"]), count)
                for row in writes:
                    self.assertEqual(
                        (self.root / row["path"]).read_text(encoding="utf-8"), row["content"]
                    )

    def test_unified_write_conflict_prevents_every_file_commit(self):
        digest = self.source("existing.py")
        action = prepare_action(
            ToolCall(
                id="write",
                name="write_files",
                arguments={
                    "writes": [
                        {"path": "new.py", "content": "NEW = True"},
                        {"path": "existing.py", "content": "VALUE = 2", "expected_hash": digest},
                    ]
                },
            ),
            self.root,
            {"existing.py": digest},
        )
        self.assertTrue(action.result.ok, action.result.summary)
        self.source("existing.py", b"CONCURRENT = True")
        result = commit_action(action, self.root, revision_id="write-conflict")
        self.assertFalse(result.ok)
        self.assertFalse((self.root / "new.py").exists())
        self.assertEqual((self.root / "existing.py").read_bytes(), b"CONCURRENT = True")

    def test_unified_write_rejects_protected_path_in_entire_batch(self):
        with patch("app.generation.engineering.actions.execute_tool_call") as tool:
            action = prepare_action(
                ToolCall(
                    id="write",
                    name="write_files",
                    arguments={
                        "writes": [
                            {"path": "new.py", "content": "NEW = True"},
                            {"path": "frontend/e2e/test.ts", "content": "tamper"},
                        ]
                    },
                ),
                self.root,
                {},
            )
        tool.assert_not_called()
        self.assertFalse(action.result.ok)
        self.assertFalse((self.root / "new.py").exists())

    def test_concurrent_change_after_tool_execution_prevents_commit(self):
        action = prepare_action(
            ToolCall(
                id="write",
                name="write_file",
                arguments={
                    "path": "new.py",
                    "content": "VALUE = 2",
                },
            ),
            self.root,
            {},
        )
        self.source("new.py", b"CONCURRENT = True")
        result = commit_action(action, self.root, revision_id="execution")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "WRITE_CONFLICT")
        self.assertEqual((self.root / "new.py").read_bytes(), b"CONCURRENT = True")

    def test_frozen_path_aliases_are_rejected_before_tool_execution(self):
        paths = [
            "frontend/e2e/test.ts",
            "./frontend/e2e/test.ts",
            "frontend\\e2e\\test.ts",
            "FRONTEND/E2E/test.ts",
            "forgeai.acceptance.json",
            "../escape.py",
            "forgeai/state.json",
        ]
        for path in paths:
            with self.subTest(path=path):
                with patch("app.generation.engineering.actions.execute_tool_call") as tool:
                    action = prepare_action(
                        ToolCall(
                            id="write",
                            name="write_file",
                            arguments={
                                "path": path,
                                "content": "tamper",
                            },
                        ),
                        self.root,
                        {},
                    )
                self.assertFalse(action.result.ok)
                tool.assert_not_called()

    def test_invalid_tool_arguments_return_observation(self):
        action = prepare_action(
            ToolCall(
                id="read",
                name="read_file",
                arguments={
                    "path": "file.py",
                    "start_line": "not an integer",
                },
            ),
            self.root,
            {},
        )
        self.assertFalse(action.result.ok)
        self.assertEqual(action.result.error_code, "TOOL_REJECTED")

    def test_image_generation_only_receives_staging_root(self):
        def image(root, **kwargs):
            self.assertNotEqual(root, self.root)
            target = root / "frontend/public/hero.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"PNG")
            return ToolExecutionResult(
                tool_call_id=kwargs["tool_call_id"],
                name="generate_image",
                ok=True,
                summary="image",
                data={"path": "frontend/public/hero.png"},
            )

        with patch("app.tools.code_engineer.images.generate_project_image", side_effect=image):
            action = prepare_action(
                ToolCall(
                    id="image",
                    name="generate_image",
                    arguments={
                        "path": "frontend/public/hero.jpg",
                        "prompt": "hero image",
                    },
                ),
                self.root,
                {},
            )
        self.assertFalse((self.root / "frontend/public/hero.png").exists())
        self.assertTrue(commit_action(action, self.root, revision_id="execution").ok)
        self.assertEqual((self.root / "frontend/public/hero.png").read_bytes(), b"PNG")

    def test_dependency_resolver_cannot_mutate_live_manifests(self):
        self.source("frontend/package.json", b'{"dependencies":{}}')

        def install(root, **kwargs):
            self.assertNotEqual(root, self.root)
            (root / "frontend/package.json").write_bytes(b'{"dependencies":{"pkg":"1"}}')
            (root / "frontend/package-lock.json").write_bytes(b'{"lockfileVersion":3}')
            return ToolExecutionResult(
                tool_call_id=kwargs["tool_call_id"],
                name="install_project_dependency",
                ok=True,
                summary="resolved",
            )

        with patch(
            "app.tools.code_engineer.project_deps.install_project_dependency", side_effect=install
        ):
            action = prepare_action(
                ToolCall(
                    id="deps",
                    name="install_project_dependency",
                    arguments={
                        "manager": "npm",
                        "action": "add",
                        "packages": ["pkg"],
                        "target": "frontend",
                    },
                ),
                self.root,
                {},
            )
        self.assertEqual((self.root / "frontend/package.json").read_bytes(), b'{"dependencies":{}}')
        self.source("frontend/package-lock.json", b"concurrent lock")
        self.assertFalse(commit_action(action, self.root, revision_id="execution").ok)
        self.assertEqual((self.root / "frontend/package.json").read_bytes(), b'{"dependencies":{}}')

    def test_partial_commit_failure_rolls_back_already_written_files(self):
        from app.generation.engineering import actions

        self.source("a.py", b"old")
        action = PreparedAction(
            ToolExecutionResult(
                tool_call_id="batch", name="install_project_dependency", ok=True, summary="staged"
            ),
            originals={"a.py": b"old", "b.py": None},
            updates={"a.py": b"new", "b.py": b"new"},
        )
        replace = actions._replace

        def fail_second(target, content):
            if target.name == "b.py":
                raise OSError("disk failure")
            return replace(target, content)

        with patch.object(actions, "_replace", side_effect=fail_second):
            result = commit_action(action, self.root, revision_id="execution")
        self.assertFalse(result.ok)
        self.assertEqual((self.root / "a.py").read_bytes(), b"old")
        self.assertFalse((self.root / "b.py").exists())
