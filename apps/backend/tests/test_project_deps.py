"""Dependency manifests are resolved in Docker and safely written back."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.generation.workspace import create_workspace
from app.tools.project_deps import (
    _manifest_snapshot,
    build_install_argv,
    dependency_docker_command,
    install_project_dependency,
    sync_manifest_dependencies,
)


class ProjectDepsTests(unittest.TestCase):
    def test_build_npm_and_uv_argv_updates_only_manifests(self) -> None:
        self.assertEqual(
            build_install_argv("npm", "add", ["echarts"]),
            [
                "npm",
                "install",
                "--package-lock-only",
                "--no-fund",
                "--no-audit",
                "--ignore-scripts",
                "echarts",
            ],
        )
        self.assertEqual(
            build_install_argv("uv", "add", ["httpx"]),
            ["uv", "add", "--no-sync", "httpx"],
        )

    def test_rejects_unsupported_managers_targets_and_shell_metacharacters(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "frontend").mkdir()
            for manager, target, packages in (
                ("npm", "frontend", ["echarts; apt install redis"]),
                ("npm", "backend", ["echarts"]),
                ("pnpm", "frontend", ["echarts"]),
                ("pip", "backend", ["httpx"]),
            ):
                with self.subTest(manager=manager, target=target):
                    result = install_project_dependency(
                        root,
                        manager=manager,
                        action="add",
                        packages=packages,
                        target=target,
                    )
                    self.assertFalse(result.ok)
                    self.assertEqual(result.error_code, "TOOL_REJECTED")

    def test_dependency_container_has_no_host_mount_or_socket(self) -> None:
        command = dependency_docker_command("docker", "trusted-image", "owned")
        for flag in (
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--pull=never",
            "--rm",
        ):
            self.assertIn(flag, command)
        self.assertNotIn("--volume", command)
        self.assertNotIn("--mount", command)
        self.assertFalse(any("docker.sock" in item for item in command))
        self.assertEqual(command[-2:], ["-I", "/opt/forgeai/dependencies.py"])

    def test_dependency_snapshot_excludes_application_source(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            frontend = root / "frontend"
            source = frontend / "src/App.tsx"
            source.parent.mkdir(parents=True)
            source.write_text("export const secret = 'not-for-resolver'", encoding="utf-8")
            (frontend / "package.json").write_text("{}", encoding="utf-8")
            (frontend / "package-lock.json").write_text("{}", encoding="utf-8")

            files, digest = _manifest_snapshot(root, "npm")

            self.assertEqual(set(files), {"frontend/package.json", "frontend/package-lock.json"})
            self.assertNotIn("not-for-resolver", json.dumps(files))
            self.assertEqual(len(digest), 64)

    def test_install_writes_only_authorized_manifest_results(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            frontend = root / "frontend"
            frontend.mkdir()
            package = frontend / "package.json"
            lock = frontend / "package-lock.json"
            package.write_text('{"dependencies": {}}', encoding="utf-8")
            lock.write_text('{"lockfileVersion": 3}', encoding="utf-8")
            updates = {
                "frontend/package.json": b'{"dependencies": {"echarts": "^6"}}',
                "frontend/package-lock.json": b'{"lockfileVersion": 3, "packages": {}}',
            }
            with patch(
                "app.tools.project_deps._run_isolated",
                return_value=(updates, "resolved", False),
            ) as isolated:
                result = install_project_dependency(
                    root,
                    manager="npm",
                    action="add",
                    packages=["echarts"],
                    target="frontend",
                )

            self.assertTrue(result.ok, result.model_dump())
            self.assertEqual(result.data["runtime"], "docker")
            self.assertIn("echarts", package.read_text(encoding="utf-8"))
            self.assertIn("packages", lock.read_text(encoding="utf-8"))
            isolated.assert_called_once()

    def test_sync_only_for_frontend_package_json(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            frontend = root / "frontend"
            frontend.mkdir()
            (frontend / "package.json").write_text("{}", encoding="utf-8")
            (frontend / "package-lock.json").write_text("{}", encoding="utf-8")
            self.assertIsNone(sync_manifest_dependencies(root, "backend/pyproject.toml"))
            with patch(
                "app.tools.project_deps._run_isolated",
                return_value=(
                    {
                        "frontend/package.json": b"{}",
                        "frontend/package-lock.json": b"{}",
                    },
                    "synced",
                    False,
                ),
            ):
                sync = sync_manifest_dependencies(root, "frontend/package.json")
            self.assertIsNotNone(sync)
            assert sync is not None
            self.assertTrue(sync.ok)

    @unittest.skipUnless(
        os.getenv("FORGEAI_TEST_DOCKER") == "1", "requires built dependency image and Docker"
    )
    def test_real_dependency_sandbox_syncs_template_lock(self) -> None:
        with TemporaryDirectory(prefix="forgeai-dependency-integration-") as directory:
            root = Path(directory) / "app"
            create_workspace(root)
            package_before = (root / "frontend/package.json").read_bytes()

            result = sync_manifest_dependencies(root, "frontend/package.json")

            self.assertIsNotNone(result)
            assert result is not None
            self.assertTrue(result.ok, result.model_dump())
            self.assertEqual(result.data["runtime"], "docker")
            self.assertEqual(
                result.data["updated_files"],
                ["frontend/package-lock.json", "frontend/package.json"],
            )
            self.assertEqual((root / "frontend/package.json").read_bytes(), package_before)
            self.assertTrue((root / "frontend/package-lock.json").is_file())


if __name__ == "__main__":
    unittest.main()
