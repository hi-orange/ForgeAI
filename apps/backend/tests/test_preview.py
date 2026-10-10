"""Local loopback preview session gates and lifecycle."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.exceptions import BusinessException
from app.core.settings import settings
from app.services import preview as preview_service
from app.services import preview_design


class PreviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        preview_service._sessions.clear()
        preview_service._reserved_ports.clear()
        self.addCleanup(preview_service._sessions.clear)
        self.addCleanup(preview_service._reserved_ports.clear)

    def test_disabled_preview_returns_disabled_status(self) -> None:
        db = MagicMock()
        user = MagicMock()
        with patch.object(settings, "preview_enabled", False):
            with patch.object(preview_service.project_service, "get_user_project"):
                status = preview_service.get_preview_status(db, user, 29)
        self.assertEqual(status.status, "disabled")

    def test_start_rejects_when_not_completed(self) -> None:
        db = MagicMock()
        user = MagicMock()
        status = MagicMock(state="engineering_running", run_id="run_1")
        with (
            patch.object(settings, "preview_enabled", True),
            patch.object(preview_service.project_service, "get_user_project"),
            patch.object(preview_service, "get_requirements_status", return_value=status),
        ):
            with self.assertRaisesRegex(BusinessException, "独立验收通过"):
                preview_service.start_preview(db, user, 29)

    def test_start_is_idempotent_while_ready(self) -> None:
        db = MagicMock()
        user = MagicMock()
        req = MagicMock(state="completed", run_id="run_abc")
        workspace = Path(tempfile.mkdtemp(prefix="forgeai-preview-"))
        (workspace / "frontend").mkdir()
        session = preview_service._PreviewSession(
            project_id=29,
            run_id="run_abc",
            workspace=workspace,
            status="ready",
            url="http://127.0.0.1:18100/",
        )
        preview_service._sessions[29] = session
        with (
            patch.object(settings, "preview_enabled", True),
            patch.object(preview_service.project_service, "get_user_project"),
            patch.object(preview_service, "get_requirements_status", return_value=req),
            patch.object(preview_service, "workspace_is_ready", return_value=True),
            patch.object(preview_service, "default_workspace_path", return_value=workspace),
            patch.object(preview_service, "_ensure_sweeper"),
            patch.object(preview_service.threading, "Thread") as thread_cls,
        ):
            first = preview_service.start_preview(db, user, 29)
            second = preview_service.start_preview(db, user, 29)
        self.assertEqual(first.status, "ready")
        self.assertEqual(first.url, "http://127.0.0.1:18100/")
        self.assertEqual(second.status, "ready")
        thread_cls.assert_not_called()

    def test_stop_clears_session(self) -> None:
        session = preview_service._PreviewSession(
            project_id=7,
            run_id="run_x",
            workspace=Path("."),
            status="ready",
            url="http://127.0.0.1:18101/",
        )
        preview_service._sessions[7] = session
        with patch.object(preview_service, "_stop_session_locked") as stop:
            out = preview_service.stop_preview(7)
        self.assertEqual(out.status, "idle")
        self.assertNotIn(7, preview_service._sessions)
        stop.assert_called_once_with(session)

    def test_allocate_port_skips_forbidden_and_falls_back(self) -> None:
        class FakeSock:
            def __init__(self, *args, **kwargs) -> None:
                self._port = 0

            def bind(self, address) -> None:
                host, port = address
                if port == 19001:
                    err = OSError("[WinError 10013] forbidden")
                    err.winerror = 10013  # type: ignore[attr-defined]
                    raise err
                if port == 0:
                    self._port = 61234
                    return
                self._port = port

            def getsockname(self):
                return ("127.0.0.1", self._port)

            def close(self) -> None:
                return None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with (
            patch.object(settings, "preview_port_min", 19001),
            patch.object(settings, "preview_port_max", 19001),
            patch.object(preview_service.socket, "socket", side_effect=lambda *a, **k: FakeSock()),
        ):
            port = preview_service._allocate_port("127.0.0.1")
        self.assertEqual(port, 61234)
        self.assertIn(61234, preview_service._reserved_ports)

    def test_allocate_port_does_not_reuse_reserved(self) -> None:
        class FakeSock:
            def __init__(self, *args, **kwargs) -> None:
                self._port = 0

            def bind(self, address) -> None:
                self._port = address[1]

            def getsockname(self):
                return ("127.0.0.1", self._port)

            def close(self) -> None:
                return None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        preview_service._reserved_ports.add(18101)
        with (
            patch.object(settings, "preview_port_min", 18101),
            patch.object(settings, "preview_port_max", 18102),
            patch.object(preview_service.socket, "socket", side_effect=lambda *a, **k: FakeSock()),
        ):
            port = preview_service._allocate_port("127.0.0.1")
        self.assertEqual(port, 18102)

    def test_frontend_preview_rebuilds_existing_dist_without_node_compile_cache(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgeai-preview-") as tmp:
            workspace = Path(tmp)
            vite = workspace / "frontend/node_modules/vite/bin/vite.js"
            swc = (
                workspace / "frontend/node_modules/@swc/core-win32-x64-msvc/swc.win32-x64-msvc.node"
            )
            dist = workspace / "frontend/dist/index.html"
            vite.parent.mkdir(parents=True)
            swc.parent.mkdir(parents=True)
            dist.parent.mkdir(parents=True)
            vite.write_text("", encoding="utf-8")
            swc.write_bytes(b"native")
            dist.write_text("old", encoding="utf-8")

            def rebuild(*args, **kwargs) -> None:
                self.assertEqual(kwargs["env"]["NODE_DISABLE_COMPILE_CACHE"], "1")
                if preview_service.os.name == "nt":
                    self.assertEqual(kwargs["env"]["SWC_BINARY_PATH"], str(swc.resolve()))
                dist.write_text("new", encoding="utf-8")

            with patch.object(preview_service, "_run_checked", side_effect=rebuild) as run:
                preview_service._ensure_frontend_dist(workspace)

            run.assert_called_once()
            self.assertEqual(dist.read_text(encoding="utf-8"), "new")
            self.assertTrue(preview_service._frontend_build_marker(workspace).is_file())

    def test_frontend_preview_reuses_dist_only_for_the_same_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgeai-preview-") as tmp:
            workspace = Path(tmp)
            frontend = workspace / "frontend"
            source = frontend / "src/App.tsx"
            dist = frontend / "dist/index.html"
            source.parent.mkdir(parents=True)
            dist.parent.mkdir(parents=True)
            source.write_text("export default function App() {}", encoding="utf-8")
            dist.write_text("built", encoding="utf-8")
            source_hash = preview_service._frontend_source_hash(frontend)
            preview_service._write_frontend_build_marker(workspace, source_hash)

            with patch.object(preview_service, "_run_checked") as run:
                preview_service._ensure_frontend_dist(workspace)

            run.assert_not_called()

    def test_frontend_source_hash_ignores_dist_and_dependencies(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgeai-preview-") as tmp:
            frontend = Path(tmp) / "frontend"
            source = frontend / "src/App.tsx"
            dependency = frontend / "node_modules/pkg/index.js"
            output = frontend / "dist/index.html"
            source.parent.mkdir(parents=True)
            dependency.parent.mkdir(parents=True)
            output.parent.mkdir(parents=True)
            source.write_text("source", encoding="utf-8")
            dependency.write_text("dependency one", encoding="utf-8")
            output.write_text("output one", encoding="utf-8")
            before = preview_service._frontend_source_hash(frontend)

            dependency.write_text("dependency two", encoding="utf-8")
            output.write_text("output two", encoding="utf-8")

            self.assertEqual(preview_service._frontend_source_hash(frontend), before)

    def test_preview_commands_decode_utf8_logs_without_host_codepage_failures(self) -> None:
        completed = MagicMock(returncode=0, stdout="完成", stderr="")
        with patch.object(preview_service.subprocess, "run", return_value=completed) as run:
            preview_service._run_checked(
                ["tool", "check"],
                cwd=Path("."),
                env={},
                timeout=10,
                label="检查",
            )

        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_backend_preview_always_reconciles_manifest_with_existing_venv(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgeai-preview-") as tmp:
            workspace = Path(tmp)
            backend = workspace / "backend"
            backend.mkdir()
            (backend / ".venv").mkdir()
            (backend / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            with patch.object(preview_service, "_run_checked") as run:
                preview_service._ensure_backend_ready(workspace, {"PATH": "test"})

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ["uv", "sync"])
        self.assertEqual(run.call_args_list[1].args[0], ["uv", "run", "alembic", "upgrade", "head"])

    def test_backend_preview_launches_the_venv_python_directly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgeai-preview-") as tmp:
            workspace = Path(tmp)
            windows_python = workspace / "backend/.venv/Scripts/python.exe"
            windows_python.parent.mkdir(parents=True)
            windows_python.write_bytes(b"")

            resolved = preview_service._backend_python(workspace)

        self.assertEqual(resolved, windows_python)

    def test_runtime_backend_failure_is_visible_in_preview_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgeai-preview-") as tmp:
            workspace = Path(tmp)
            log_path = workspace / "forgeai/logs/preview-backend.log"
            log_path.parent.mkdir(parents=True)
            log_path.write_text(
                f"Traceback at {workspace}\\backend\\app.py\nZoneInfoNotFoundError: missing",
                encoding="utf-8",
            )
            session = preview_service._PreviewSession(
                project_id=29,
                run_id="run_logs",
                workspace=workspace,
                status="ready",
                url="http://127.0.0.1:18100/",
                backend_log_path=log_path,
            )

            preview_service._record_backend_failure(session, "/api/v1/profile", 500)

            preview_service._clear_backend_failure(session, "/api/v1/menu/items")
            self.assertIn("/api/v1/profile", session.message or "")
            self.assertIn("ZoneInfoNotFoundError", session.message or "")
            self.assertNotIn(str(workspace.resolve()), session.message or "")
            preview_service._clear_backend_failure(session, "/api/v1/profile")

        self.assertEqual(session.status, "ready")
        self.assertIsNone(session.message)
        self.assertIsNone(session.backend_error_path)

    def test_backend_diagnostic_keeps_root_cause_at_the_end(self) -> None:
        detail = "x" * 5_000 + "ROOT_CAUSE"
        message = preview_service._with_backend_diagnostic("API failed", detail)

        self.assertLessEqual(len(message), preview_service._MAX_RUNTIME_DIAGNOSTIC_CHARS)
        self.assertTrue(message.endswith("ROOT_CAUSE"))

    def test_design_state_is_versioned_and_recoverable(self) -> None:
        db = MagicMock()
        user = MagicMock()
        status = MagicMock(state="completed", run_id="run_design")
        with tempfile.TemporaryDirectory(prefix="forgeai-design-") as tmp:
            workspace = Path(tmp)
            with (
                patch.object(preview_design.project_service, "get_user_project"),
                patch.object(preview_design, "get_requirements_status", return_value=status),
                patch.object(preview_design, "workspace_is_ready", return_value=True),
                patch.object(preview_design, "default_workspace_path", return_value=workspace),
            ):
                initial = preview_design.get_design_state(db, user, 29)
                self.assertEqual(initial.revision, 0)
                update = preview_design.PreviewDesignUpdate(
                    theme={**initial.theme.model_dump(), "name": "My Theme"},
                    elements=[
                        {
                            "selector": "#hero",
                            "label": "Hero",
                            "styles": {"padding": "32px", "borderRadius": "16px"},
                            "text": "Welcome",
                        }
                    ],
                )
                first = preview_design.save_design_state(db, user, 29, update)
                second = preview_design.save_design_state(db, user, 29, update)
                loaded = preview_design.get_design_state(db, user, 29)

            self.assertEqual((first.revision, second.revision, loaded.revision), (1, 2, 2))
            self.assertEqual(loaded.elements[0].text, "Welcome")
            self.assertTrue((workspace / "forgeai/design/revisions/000001.json").is_file())
            self.assertTrue((workspace / "forgeai/design/revisions/000002.json").is_file())
            self.assertTrue((workspace / "forgeai/design/current.json").is_file())

    def test_design_selector_accepts_preview_bridge_child_combinators(self) -> None:
        selector = "body > main:nth-of-type(1) > section:nth-of-type(1) > h1:nth-of-type(1)"
        override = preview_design.PreviewElementOverride(selector=selector)

        self.assertEqual(override.selector, selector)
        with self.assertRaises(ValueError):
            preview_design.PreviewElementOverride(selector="body > <script>")

    def test_design_rejects_unfinished_build(self) -> None:
        with (
            patch.object(preview_design.project_service, "get_user_project"),
            patch.object(
                preview_design,
                "get_requirements_status",
                return_value=MagicMock(state="engineering_running", run_id="run_design"),
            ),
            self.assertRaisesRegex(BusinessException, "独立验收通过"),
        ):
            preview_design.get_design_state(MagicMock(), MagicMock(), 29)

    def test_design_bridge_embeds_saved_state_without_script_breakout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgeai-design-") as tmp:
            workspace = Path(tmp)
            root = workspace / "forgeai/design"
            root.mkdir(parents=True)
            state = preview_design.PreviewDesignState(
                revision=1,
                elements=[
                    {
                        "selector": "#hero",
                        "label": "Hero",
                        "styles": {"color": "#112233"},
                        "text": "safe </script><script>alert(1)</script>",
                    }
                ],
            )
            (root / "current.json").write_text(state.model_dump_json(), encoding="utf-8")
            output = preview_design.inject_design_bridge(
                b"<html><body>Hello</body></html>", workspace
            )

        html = output.decode("utf-8")
        self.assertIn("forgeai-preview-design", html)
        self.assertIn("#112233", html)
        self.assertNotIn("safe </script><script>", html)
        self.assertLess(html.index("forgeai-preview-design"), html.index("</body>"))

    def test_design_save_never_overwrites_an_existing_revision(self) -> None:
        db = MagicMock()
        user = MagicMock()
        status = MagicMock(state="completed", run_id="run_design")
        with tempfile.TemporaryDirectory(prefix="forgeai-design-") as tmp:
            workspace = Path(tmp)
            revisions = workspace / "forgeai/design/revisions"
            revisions.mkdir(parents=True)
            existing = revisions / "000001.json"
            existing.write_text("historical revision", encoding="utf-8")
            with (
                patch.object(preview_design.project_service, "get_user_project"),
                patch.object(preview_design, "get_requirements_status", return_value=status),
                patch.object(preview_design, "workspace_is_ready", return_value=True),
                patch.object(preview_design, "default_workspace_path", return_value=workspace),
            ):
                saved = preview_design.save_design_state(
                    db, user, 29, preview_design.PreviewDesignUpdate()
                )

            self.assertEqual(saved.revision, 2)
            self.assertEqual(existing.read_text(encoding="utf-8"), "historical revision")
            self.assertTrue((revisions / "000002.json").is_file())


if __name__ == "__main__":
    unittest.main()
