"""Bounded project dependency installs reject system packages and unsafe specs."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.tools.project_deps import (
    build_install_argv,
    install_project_dependency,
    sync_manifest_dependencies,
)


class ProjectDepsTests(unittest.TestCase):
    def test_build_npm_and_uv_argv(self) -> None:
        self.assertEqual(
            build_install_argv("npm", "add", ["echarts", "vue-router@^4.5.0"]),
            [
                "npm",
                "install",
                "--no-fund",
                "--no-audit",
                "--ignore-scripts",
                "echarts",
                "vue-router@^4.5.0",
            ],
        )
        self.assertEqual(
            build_install_argv("uv", "add", ["httpx"]),
            ["uv", "add", "httpx"],
        )

    def test_rejects_system_package_managers_and_shell_metacharacters(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "frontend").mkdir()
            bad = install_project_dependency(
                root,
                manager="npm",
                action="add",
                packages=["echarts; apt install redis"],
                target="frontend",
            )
            self.assertFalse(bad.ok)
            self.assertEqual(bad.error_code, "TOOL_REJECTED")

            wrong_target = install_project_dependency(
                root,
                manager="npm",
                action="add",
                packages=["echarts"],
                target="backend",
            )
            self.assertFalse(wrong_target.ok)

    def test_install_invokes_subprocess_without_shell(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "frontend").mkdir()
            completed = SimpleNamespace(returncode=0, stdout="added\n", stderr="")
            with (
                patch("app.tools.project_deps.shutil.which", return_value="/usr/bin/npm"),
                patch("app.tools.project_deps.subprocess.run", return_value=completed) as run,
            ):
                result = install_project_dependency(
                    root,
                    manager="npm",
                    action="add",
                    packages=["echarts"],
                    target="frontend",
                )
            self.assertTrue(result.ok)
            self.assertEqual(run.call_args.kwargs.get("shell"), False)
            self.assertEqual(run.call_args.kwargs.get("cwd"), (root / "frontend").resolve())
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], "/usr/bin/npm")
            self.assertIn("echarts", argv)

    def test_sync_only_for_frontend_package_json(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "frontend").mkdir()
            self.assertIsNone(sync_manifest_dependencies(root, "backend/pyproject.toml"))
            completed = SimpleNamespace(returncode=0, stdout="synced\n", stderr="")
            with (
                patch("app.tools.project_deps.shutil.which", return_value="/usr/bin/npm"),
                patch("app.tools.project_deps.subprocess.run", return_value=completed),
            ):
                sync = sync_manifest_dependencies(root, "frontend/package.json")
            self.assertIsNotNone(sync)
            assert sync is not None
            self.assertTrue(sync.ok)


if __name__ == "__main__":
    unittest.main()
