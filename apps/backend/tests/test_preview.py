"""Local loopback preview session gates and lifecycle."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.exceptions import BusinessException
from app.core.settings import settings
from app.services import preview as preview_service


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
        preview_service._reserved_ports.add(18101)
        with (
            patch.object(settings, "preview_port_min", 18101),
            patch.object(settings, "preview_port_max", 18102),
        ):
            port = preview_service._allocate_port("127.0.0.1")
        self.assertEqual(port, 18102)


if __name__ == "__main__":
    unittest.main()
