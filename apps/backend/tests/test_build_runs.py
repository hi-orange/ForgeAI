import re
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.api.v1.build_runs import router
from app.api.v1.router import api_router
from app.core.exceptions import ConflictException, register_exception_handlers
from app.db.database import Base, get_db
from app.models.build_run import BuildRun, BuildRunStage, BuildRunStatus
from app.models.project import Project, ProjectStatus
from app.models.user import User
from app.services import build_run as build_run_service


class BuildRunApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

        with self.session_factory() as db:
            self.user = User(
                username="owner",
                email="owner@example.com",
                hashed_password="not-used",
            )
            other_user = User(
                username="other",
                email="other@example.com",
                hashed_password="not-used",
            )
            db.add_all((self.user, other_user))
            db.flush()
            self.project = Project(
                user_id=self.user.id,
                name="Demo",
                prompt="Build a demo app",
                status=ProjectStatus.DRAFT.value,
            )
            self.other_project = Project(
                user_id=other_user.id,
                name="Other",
                prompt="Build another app",
                status=ProjectStatus.DRAFT.value,
            )
            db.add_all((self.project, self.other_project))
            db.commit()
            self.other_user = other_user

        self.current_user = self.user
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(router, prefix="/api/v1")

        def override_db():
            with self.session_factory() as db:
                yield db

        def override_current_user() -> User:
            return self.current_user

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = override_current_user
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _create_run(self, project_id: int | None = None):
        return self.client.post(f"/api/v1/projects/{project_id or self.project.id}/build-runs")

    def _create_owner_project(self, name: str) -> int:
        with self.session_factory() as db:
            project = Project(
                user_id=self.user.id,
                name=name,
                prompt=f"Build {name}",
                status=ProjectStatus.DRAFT.value,
            )
            db.add(project)
            db.commit()
            return project.id

    def _mark_run_terminal(self, run_id: str, status: BuildRunStatus) -> None:
        with self.session_factory() as db:
            build_run = db.scalar(select(BuildRun).where(BuildRun.run_id == run_id))
            self.assertIsNotNone(build_run)
            build_run.status = status.value
            build_run.active_slot = None
            db.commit()

    def test_create_and_get_build_run(self) -> None:
        response = self._create_run()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["code"], 0)
        self.assertEqual(payload["msg"], "构建任务已进入队列")
        run = payload["data"]
        self.assertRegex(run["run_id"], re.compile(r"^run_[0-9a-f]{32}$"))
        self.assertEqual(run["project_id"], self.project.id)
        self.assertEqual(run["status"], BuildRunStatus.QUEUED.value)
        self.assertIsNone(run["stage"])
        self.assertIsNone(run["error"])
        self.assertNotIn("active_slot", run)
        self.assertNotIn("id", run)

        get_response = self.client.get(
            f"/api/v1/projects/{self.project.id}/build-runs/{run['run_id']}"
        )
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json()["data"], run)

        with self.session_factory() as db:
            project = db.get(Project, self.project.id)
            self.assertIsNotNone(project)
            self.assertEqual(project.status, ProjectStatus.DRAFT.value)

    def test_create_build_run_preserves_available_project_status(self) -> None:
        project_id = self._create_owner_project("Available")
        with self.session_factory() as db:
            project = db.get(Project, project_id)
            self.assertIsNotNone(project)
            project.status = ProjectStatus.AVAILABLE.value
            db.commit()

        response = self._create_run(project_id)

        self.assertEqual(response.status_code, 202)
        with self.session_factory() as db:
            project = db.get(Project, project_id)
            self.assertIsNotNone(project)
            self.assertEqual(project.status, ProjectStatus.AVAILABLE.value)

    def test_rejects_second_active_build_run(self) -> None:
        self.assertEqual(self._create_run().status_code, 202)

        response = self._create_run()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["msg"], "项目已有构建任务正在运行")
        with self.session_factory() as db:
            count = db.scalar(
                select(func.count())
                .select_from(BuildRun)
                .where(BuildRun.project_id == self.project.id)
            )
            self.assertEqual(count, 1)

    def test_running_build_run_remains_active(self) -> None:
        first = self._create_run().json()["data"]
        with self.session_factory() as db:
            build_run = db.scalar(select(BuildRun).where(BuildRun.run_id == first["run_id"]))
            self.assertIsNotNone(build_run)
            build_run.status = BuildRunStatus.RUNNING.value
            build_run.stage = BuildRunStage.PM.value
            db.commit()

        response = self._create_run()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["msg"], "项目已有构建任务正在运行")

    def test_terminal_history_allows_a_new_run(self) -> None:
        for terminal_status in (BuildRunStatus.SUCCEEDED, BuildRunStatus.FAILED):
            with self.subTest(status=terminal_status.value):
                project_id = self._create_owner_project(terminal_status.value)
                first = self._create_run(project_id).json()["data"]
                self._mark_run_terminal(first["run_id"], terminal_status)

                second = self._create_run(project_id)

                self.assertEqual(second.status_code, 202)
                self.assertNotEqual(second.json()["data"]["run_id"], first["run_id"])

    def test_different_projects_can_each_have_an_active_run(self) -> None:
        second_project_id = self._create_owner_project("Second")

        first = self._create_run()
        second = self._create_run(second_project_id)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)

        cross_project = self.client.get(
            f"/api/v1/projects/{second_project_id}/build-runs/{first.json()['data']['run_id']}"
        )
        self.assertEqual(cross_project.status_code, 404)
        self.assertEqual(cross_project.json()["msg"], "构建任务不存在")

    def test_project_ownership_is_not_leaked(self) -> None:
        response = self._create_run(self.other_project.id)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["msg"], "项目不存在")

        own_run = self._create_run().json()["data"]
        self.current_user = self.other_user
        get_response = self.client.get(
            f"/api/v1/projects/{self.project.id}/build-runs/{own_run['run_id']}"
        )
        self.assertEqual(get_response.status_code, 404)
        self.assertEqual(get_response.json()["msg"], "项目不存在")

    def test_unknown_build_run_returns_not_found(self) -> None:
        response = self.client.get(f"/api/v1/projects/{self.project.id}/build-runs/run_missing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["msg"], "构建任务不存在")

    def test_build_run_routes_are_registered_on_v1_api(self) -> None:
        registered_app = FastAPI()
        registered_app.include_router(api_router)
        paths = registered_app.openapi()["paths"]

        self.assertIn("post", paths["/api/v1/projects/{project_id}/build-runs"])
        self.assertIn("get", paths["/api/v1/projects/{project_id}/build-runs/{run_id}"])

    def test_database_rejects_two_active_slots_for_one_project(self) -> None:
        with self.session_factory() as db:
            db.add_all(
                (
                    BuildRun(project_id=self.project.id, run_id="run_a", active_slot=1),
                    BuildRun(project_id=self.project.id, run_id="run_b", active_slot=1),
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

    def test_database_rejects_running_without_stage(self) -> None:
        project_id = self._create_owner_project("No stage")
        with self.session_factory() as db:
            db.add(
                BuildRun(
                    project_id=project_id,
                    run_id="run_without_stage",
                    status=BuildRunStatus.RUNNING.value,
                    active_slot=1,
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

    def test_database_rejects_unknown_project_status(self) -> None:
        with self.session_factory() as db:
            db.add(
                Project(
                    user_id=self.user.id,
                    name="Unknown status",
                    prompt="Build invalid status",
                    status="building",
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

    def test_project_delete_cascades_to_build_runs(self) -> None:
        run_id = self._create_run().json()["data"]["run_id"]
        with self.session_factory() as db:
            project = db.get(Project, self.project.id)
            self.assertIsNotNone(project)
            db.delete(project)
            db.commit()
            self.assertIsNone(db.scalar(select(BuildRun).where(BuildRun.run_id == run_id)))

    def test_integrity_race_is_reported_as_conflict(self) -> None:
        db = MagicMock(spec=Session)
        db.commit.side_effect = IntegrityError("insert", {}, Exception("duplicate"))
        active = BuildRun(
            project_id=self.project.id,
            run_id="run_existing",
            status=BuildRunStatus.QUEUED.value,
            active_slot=1,
        )

        with (
            patch.object(
                build_run_service.project_service, "get_user_project", return_value=self.project
            ),
            patch.object(build_run_service, "_active_build_run", side_effect=(None, active)),
            self.assertRaises(ConflictException),
        ):
            build_run_service.create_build_run(db, self.user, self.project.id)

        db.rollback.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
