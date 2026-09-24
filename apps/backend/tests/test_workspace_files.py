"""Workspace file listing/read after requirements approval."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import ConflictException
from app.core.settings import settings
from app.db.database import Base
from app.generation.workspace import prepare_engineering_workspace
from app.models.build_run import BuildRun
from app.models.project import Project
from app.models.user import User
from app.services import workspace_files as workspace_files_service


class WorkspaceFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="forgeai-ws-files-"))
        self.workspace_root = self.enterContext(tempfile.TemporaryDirectory(prefix="forgeai-ws-"))
        self.enterContext(patch.object(settings, "runtime_data_root", self.workspace_root))
        self.engine = create_engine(
            f"sqlite+pysqlite:///{Path(directory) / 'ws.db'}",
            connect_args={"check_same_thread": False},
        )
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.session_factory() as db:
            user = User(username="ws-owner", email="ws@test.com", hashed_password="x")
            db.add(user)
            db.flush()
            project = Project(user_id=user.id, name="WS", prompt="做招聘站", status="draft")
            db.add(project)
            db.flush()
            run = BuildRun(
                project_id=project.id,
                run_id="run_workspace_files",
                status="running",
                stage="pm",
                active_slot=1,
            )
            db.add(run)
            db.commit()
            self.user_id = user.id
            self.project_id = project.id
            self.run_id = run.run_id

    def _owner(self) -> User:
        with self.session_factory() as db:
            return db.get(User, self.user_id)

    def test_list_and_read_after_prepare(self) -> None:
        prepare_engineering_workspace(
            self.project_id,
            self.run_id,
            task_id="task_ws",
            approved_item_id="ci_ws",
            app_spec={"goal": "招聘站"},
        )
        with self.session_factory() as db:
            with patch("app.services.workspace_files.get_requirements_status") as status:
                from app.schemas.requirements import RequirementsStatus

                status.return_value = RequirementsStatus(
                    project_id=self.project_id,
                    run_id=self.run_id,
                    state="engineering_generated",
                    workspace_ready=True,
                    code_ready=True,
                )
                listing = workspace_files_service.list_project_workspace(
                    db, self._owner(), self.project_id
                )
                self.assertTrue(listing.ready)
                paths = {item.path for item in listing.files}
                self.assertIn("frontend/src/App.vue", paths)
                self.assertIn("backend/app/main.py", paths)
                self.assertTrue(all(not path.startswith("forgeai/") for path in paths))
                content = workspace_files_service.read_project_workspace_file(
                    db, self._owner(), self.project_id, "backend/app/api/health.py"
                )
                self.assertIn("health", content.content)
                with self.assertRaises(ConflictException):
                    workspace_files_service.read_project_workspace_file(
                        db, self._owner(), self.project_id, "../secret.txt"
                    )

    def test_list_exposes_template_as_soon_as_workspace_is_ready(self) -> None:
        prepare_engineering_workspace(
            self.project_id,
            self.run_id,
            task_id="task_ws",
            approved_item_id="ci_ws",
            app_spec={"goal": "招聘站"},
        )
        with self.session_factory() as db:
            with patch("app.services.workspace_files.get_requirements_status") as status:
                from app.schemas.requirements import RequirementsStatus

                status.return_value = RequirementsStatus(
                    project_id=self.project_id,
                    run_id=self.run_id,
                    state="engineering_running",
                    workspace_ready=True,
                    code_ready=False,
                )
                listing = workspace_files_service.list_project_workspace(
                    db, self._owner(), self.project_id
                )
                self.assertTrue(listing.ready)
                self.assertIn("frontend/src/App.vue", {item.path for item in listing.files})
                content = workspace_files_service.read_project_workspace_file(
                    db, self._owner(), self.project_id, "frontend/src/App.vue"
                )
                self.assertIn("template", content.content)


class WorkspaceReadyOnPauseTests(unittest.TestCase):
    def test_retry_and_stopped_still_expose_workspace(self) -> None:
        from unittest.mock import MagicMock

        from app.schemas.requirements import RequirementsStatus
        from app.services.requirements import _attach_workspace_status

        for state in ("retry_available", "stopped"):
            status = RequirementsStatus(project_id=9, run_id="run_pause", state=state)
            with (
                patch("app.services.requirements.workspace_is_ready", return_value=True),
                patch.object(settings, "runtime_data_root", "runtime-data"),
            ):
                out = _attach_workspace_status(MagicMock(), status)
            self.assertTrue(out.workspace_ready, state)
            self.assertTrue(out.workspace_path)


if __name__ == "__main__":
    unittest.main()
