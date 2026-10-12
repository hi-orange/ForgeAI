"""Local loopback preview session gates and lifecycle."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.exceptions import BusinessException
from app.core.settings import settings
from app.runtime.preview import PreviewSandboxHandle
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

    def test_start_rejects_when_workspace_is_not_ready(self) -> None:
        db = MagicMock()
        user = MagicMock()
        status = MagicMock(
            state="engineering_running",
            run_id="run_1",
            workspace_ready=False,
            code_item_id=None,
        )
        with (
            patch.object(settings, "preview_enabled", True),
            patch.object(preview_service.project_service, "get_user_project"),
            patch.object(preview_service, "get_requirements_status", return_value=status),
        ):
            with self.assertRaisesRegex(BusinessException, "工作区尚未就绪"):
                preview_service.start_preview(db, user, 29)

    def test_start_allows_unverified_engineering_workspace(self) -> None:
        db = MagicMock()
        user = MagicMock()
        req = MagicMock(
            state="engineering_running",
            run_id="run_working",
            preview_revision=None,
            preview_source_hash=None,
            workspace_ready=True,
            code_item_id=None,
        )
        workspace = Path(tempfile.mkdtemp(prefix="forgeai-preview-working-"))
        (workspace / "frontend").mkdir()
        with (
            patch.object(settings, "preview_enabled", True),
            patch.object(preview_service.project_service, "get_user_project"),
            patch.object(preview_service, "get_requirements_status", return_value=req),
            patch.object(preview_service, "workspace_is_ready", return_value=True),
            patch.object(preview_service, "default_workspace_path", return_value=workspace),
            patch.object(preview_service, "source_snapshot", return_value=(b"{}", "a" * 64)),
            patch.object(preview_service, "_ensure_sweeper"),
            patch.object(preview_service.threading, "Thread") as thread_cls,
        ):
            result = preview_service.start_preview(db, user, 29)

        self.assertEqual(result.status, "starting")
        self.assertFalse(result.verified)
        self.assertEqual(preview_service._sessions[29].run_id, "run_working")
        thread_cls.assert_called_once()

    def test_start_uses_exact_published_revision_snapshot(self) -> None:
        db = MagicMock()
        user = MagicMock()
        req = MagicMock(
            state="engineering_running",
            run_id="run_revision",
            workspace_ready=True,
            code_item_id=None,
            preview_revision=3,
            preview_source_hash="a" * 64,
        )
        event = MagicMock(payload={"source_artifact_id": "src_revision", "source_hash": "a" * 64})
        db.scalar.return_value = event
        workspace = Path(tempfile.mkdtemp(prefix="forgeai-preview-revision-"))
        (workspace / "frontend").mkdir()
        with (
            patch.object(settings, "preview_enabled", True),
            patch.object(preview_service.project_service, "get_user_project"),
            patch.object(preview_service, "get_requirements_status", return_value=req),
            patch.object(preview_service, "workspace_is_ready", return_value=True),
            patch.object(preview_service, "default_workspace_path", return_value=workspace),
            patch.object(
                preview_service,
                "read_source_artifact_snapshot",
                return_value=b'{"frontend/index.html":"revision"}',
            ) as read_snapshot,
            patch.object(preview_service, "source_snapshot") as live_snapshot,
            patch.object(preview_service, "_ensure_sweeper"),
            patch.object(preview_service.threading, "Thread"),
        ):
            result = preview_service.start_preview(db, user, 29)

        self.assertEqual(result.revision, 3)
        self.assertEqual(result.source_hash, "a" * 64)
        session = preview_service._sessions[29]
        self.assertEqual(session.snapshot_bytes, b'{"frontend/index.html":"revision"}')
        read_snapshot.assert_called_once_with(
            db,
            project_id=29,
            artifact_id="src_revision",
            source_hash="a" * 64,
        )
        live_snapshot.assert_not_called()

    def test_start_is_idempotent_while_ready(self) -> None:
        db = MagicMock()
        user = MagicMock()
        req = MagicMock(
            state="completed",
            run_id="run_abc",
            preview_revision=None,
            preview_source_hash=None,
            code_item_id="ci_preview_code",
            workspace_ready=True,
        )
        workspace = Path(tempfile.mkdtemp(prefix="forgeai-preview-"))
        (workspace / "frontend").mkdir()
        session = preview_service._PreviewSession(
            project_id=29,
            run_id="run_abc",
            workspace=workspace,
            status="ready",
            url="http://127.0.0.1:18100/",
            runtime_handle=PreviewSandboxHandle(
                sandbox_id="forgeai-preview-existing",
                source_hash="a" * 64,
                host="127.0.0.1",
                port=18199,
                log_path=workspace / "preview.log",
            ),
        )
        preview_service._sessions[29] = session
        with (
            patch.object(settings, "preview_enabled", True),
            patch.object(preview_service.project_service, "get_user_project"),
            patch.object(preview_service, "get_requirements_status", return_value=req),
            patch.object(preview_service, "workspace_is_ready", return_value=True),
            patch.object(preview_service, "default_workspace_path", return_value=workspace),
            patch.object(
                preview_service,
                "ensure_code_workspace",
                return_value=(workspace, False),
            ),
            patch.object(preview_service, "_ensure_sweeper"),
            patch.object(preview_service, "source_snapshot", return_value=(b"{}", "a" * 64)),
            patch.object(preview_service.threading, "Thread") as thread_cls,
        ):
            first = preview_service.start_preview(db, user, 29)
            second = preview_service.start_preview(db, user, 29)
        self.assertEqual(first.status, "ready")
        self.assertEqual(first.url, "http://127.0.0.1:18100/")
        self.assertEqual(second.status, "ready")
        thread_cls.assert_not_called()
        self.assertTrue(second.verified)

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

    def test_boot_session_runs_generated_app_only_through_sandbox_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgeai-preview-") as tmp:
            workspace = Path(tmp)
            handle = PreviewSandboxHandle(
                sandbox_id="forgeai-preview-test",
                source_hash="a" * 64,
                host="127.0.0.1",
                port=18201,
                log_path=workspace / "forgeai/logs/preview-sandbox.log",
            )
            runtime = MagicMock()
            runtime.start.return_value = handle
            runtime.is_running.return_value = True
            proxy = MagicMock()
            session = preview_service._PreviewSession(
                project_id=29,
                run_id="run_sandbox",
                workspace=workspace,
            )
            with (
                patch.object(preview_service, "get_preview_runtime", return_value=runtime),
                patch.object(preview_service, "_allocate_port", side_effect=[18201, 18202]),
                patch.object(preview_service, "_wait_http") as wait_http,
                patch.object(preview_service, "_start_proxy", return_value=proxy),
            ):
                preview_service._boot_session(session)

        request = runtime.start.call_args.args[0]
        self.assertEqual(request.workspace, workspace)
        self.assertEqual(request.host_port, 18201)
        self.assertEqual(session.status, "ready")
        self.assertEqual(session.url, "http://127.0.0.1:18202/")
        self.assertIs(session.runtime_handle, handle)
        self.assertEqual(wait_http.call_count, 3)
        runtime.stop.assert_not_called()

    def test_runtime_backend_failure_is_visible_in_preview_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgeai-preview-") as tmp:
            workspace = Path(tmp)
            log_path = workspace / "forgeai/logs/preview-sandbox.log"
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
                preview_log_path=log_path,
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
