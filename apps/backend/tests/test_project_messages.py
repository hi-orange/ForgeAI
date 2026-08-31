import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    event,
    func,
    inspect,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.api.v1.project_messages import router
from app.api.v1.router import api_router
from app.core.exceptions import register_exception_handlers
from app.db.database import Base, get_db
from app.models.project import Project, ProjectStatus
from app.models.project_message import ProjectMessage, ProjectMessageSender
from app.models.user import User
from app.schemas.project import ProjectCreate
from app.schemas.project_message import ProjectMessageCreate
from app.services import project as project_service
from app.services import project_message as project_message_service


class ProjectMessageApiTests(unittest.TestCase):
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
            self.other_user = User(
                username="other",
                email="other@example.com",
                hashed_password="not-used",
            )
            db.add_all((self.user, self.other_user))
            db.flush()
            self.project = Project(
                user_id=self.user.id,
                name="Demo",
                prompt="Build a demo app",
                status=ProjectStatus.DRAFT.value,
            )
            self.other_project = Project(
                user_id=self.other_user.id,
                name="Other",
                prompt="Build another app",
                status=ProjectStatus.DRAFT.value,
            )
            db.add_all((self.project, self.other_project))
            db.commit()

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

    def _post_message(
        self,
        client_message_id: str,
        content: str,
        *,
        project_id: int | None = None,
    ):
        return self.client.post(
            f"/api/v1/projects/{project_id or self.project.id}/messages",
            json={"client_message_id": client_message_id, "content": content},
        )

    def test_create_and_list_messages_in_sequence_order(self) -> None:
        first_response = self._post_message("msg-1", "  first message  ")
        second_response = self._post_message("msg-2", "second message")

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        first = first_response.json()["data"]
        second = second_response.json()["data"]
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(second["sequence"], 2)
        self.assertEqual(first["sender"], ProjectMessageSender.USER.value)
        self.assertEqual(first["content"], "first message")

        list_response = self.client.get(f"/api/v1/projects/{self.project.id}/messages")

        self.assertEqual(list_response.status_code, 200)
        messages = list_response.json()["data"]
        self.assertEqual([message["id"] for message in messages], [first["id"], second["id"]])
        self.assertEqual([message["sequence"] for message in messages], [1, 2])

        page_response = self.client.get(
            f"/api/v1/projects/{self.project.id}/messages",
            params={"after_sequence": 1, "limit": 1},
        )
        self.assertEqual(page_response.status_code, 200)
        self.assertEqual(
            [message["id"] for message in page_response.json()["data"]],
            [second["id"]],
        )

    def test_same_client_message_id_is_idempotent(self) -> None:
        first = self._post_message("retry-key", "same content")
        replay = self._post_message("retry-key", "same content")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["data"], first.json()["data"])
        with self.session_factory() as db:
            count = db.scalar(
                select(func.count())
                .select_from(ProjectMessage)
                .where(ProjectMessage.project_id == self.project.id)
            )
            self.assertEqual(count, 1)

    def test_reusing_client_message_id_with_different_content_conflicts(self) -> None:
        self.assertEqual(self._post_message("collision", "first").status_code, 200)

        response = self._post_message("collision", "different")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["msg"], "client_message_id 已用于另一条消息")

    def test_project_ownership_is_not_leaked(self) -> None:
        create_response = self._post_message(
            "foreign",
            "not allowed",
            project_id=self.other_project.id,
        )
        list_response = self.client.get(f"/api/v1/projects/{self.other_project.id}/messages")

        self.assertEqual(create_response.status_code, 404)
        self.assertEqual(create_response.json()["msg"], "项目不存在")
        self.assertEqual(list_response.status_code, 404)
        self.assertEqual(list_response.json()["msg"], "项目不存在")

        self.assertEqual(self._post_message("owned", "owner content").status_code, 200)
        self.current_user = self.other_user
        hidden_response = self.client.get(f"/api/v1/projects/{self.project.id}/messages")
        self.assertEqual(hidden_response.status_code, 404)
        self.assertEqual(hidden_response.json()["msg"], "项目不存在")

    def test_create_project_persists_initial_prompt_as_first_message(self) -> None:
        with self.session_factory() as db:
            project = project_service.create_project(
                db,
                self.user,
                ProjectCreate(prompt="  Build a task tracker  ", name="Tasks"),
            )
            initial = db.scalar(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
            self.assertIsNotNone(initial)
            self.assertEqual(initial.sequence, 1)
            self.assertEqual(initial.sender, ProjectMessageSender.USER.value)
            self.assertEqual(initial.content, "Build a task tracker")
            self.assertEqual(initial.client_message_id, f"project:{project.id}:initial")

        response = self._post_message("follow-up", "Add sharing", project_id=project.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["sequence"], 2)

    def test_project_delete_cascades_to_messages(self) -> None:
        message_id = self._post_message("delete-me", "temporary").json()["data"]["id"]

        with self.session_factory() as db:
            project = db.get(Project, self.project.id)
            self.assertIsNotNone(project)
            db.delete(project)
            db.commit()
            self.assertIsNone(db.get(ProjectMessage, message_id))

    def test_database_rejects_duplicate_sequence_and_client_id(self) -> None:
        with self.session_factory() as db:
            db.add(
                ProjectMessage(
                    project_id=self.project.id,
                    sequence=1,
                    sender=ProjectMessageSender.USER.value,
                    content="first",
                    client_message_id="db-key-1",
                )
            )
            db.commit()

            db.add(
                ProjectMessage(
                    project_id=self.project.id,
                    sequence=1,
                    sender=ProjectMessageSender.USER.value,
                    content="duplicate sequence",
                    client_message_id="db-key-2",
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

            db.add(
                ProjectMessage(
                    project_id=self.project.id,
                    sequence=2,
                    sender=ProjectMessageSender.USER.value,
                    content="duplicate client id",
                    client_message_id="db-key-1",
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

    def test_migration_backfills_existing_project_prompt(self) -> None:
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "0f4e8b7a2c1d_create_project_message.py"
        )
        spec = importlib.util.spec_from_file_location("project_message_migration", migration_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        legacy_engine = create_engine("sqlite+pysqlite://")
        metadata = MetaData()
        project_table = Table(
            "project",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("prompt", Text),
            Column("created_at", DateTime, nullable=False),
        )
        metadata.create_all(legacy_engine)
        created_at = datetime(2026, 8, 31, 12, 0, 0)

        with legacy_engine.begin() as connection:
            connection.execute(
                project_table.insert(),
                [
                    {"id": 7, "prompt": "  Existing requirement  ", "created_at": created_at},
                    {"id": 8, "prompt": None, "created_at": created_at},
                ],
            )
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            reflected = Table("project_message", MetaData(), autoload_with=connection)
            rows = connection.execute(select(reflected)).mappings().all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["project_id"], 7)
            self.assertEqual(rows[0]["sequence"], 1)
            self.assertEqual(rows[0]["sender"], ProjectMessageSender.USER.value)
            self.assertEqual(rows[0]["content"], "Existing requirement")
            self.assertEqual(rows[0]["client_message_id"], "project:7:initial")

            migration.downgrade()
            self.assertNotIn("project_message", inspect(connection).get_table_names())

        legacy_engine.dispose()

    def test_sequence_collision_is_retried(self) -> None:
        db = MagicMock(spec=Session)
        db.commit.side_effect = (
            IntegrityError("insert", {}, Exception("duplicate sequence")),
            None,
        )
        payload = ProjectMessageCreate(client_message_id="race", content="concurrent")

        with (
            patch.object(
                project_message_service.project_service,
                "get_user_project",
                return_value=self.project,
            ),
            patch.object(
                project_message_service,
                "_find_by_client_message_id",
                side_effect=(None, None),
            ),
            patch.object(project_message_service, "_next_sequence", side_effect=(1, 2)),
        ):
            message = project_message_service.create_user_project_message(
                db,
                self.user,
                self.project.id,
                payload,
            )

        self.assertEqual(message.sequence, 2)
        self.assertEqual(db.add.call_count, 2)
        db.rollback.assert_called_once_with()

    def test_api_rejects_blank_content_and_sender_override(self) -> None:
        blank = self._post_message("blank", "   ")
        sender_override = self.client.post(
            f"/api/v1/projects/{self.project.id}/messages",
            json={
                "client_message_id": "spoofed",
                "content": "hello",
                "sender": ProjectMessageSender.ASSISTANT.value,
            },
        )

        self.assertEqual(blank.status_code, 422)
        self.assertEqual(sender_override.status_code, 422)

    def test_project_message_routes_are_registered_on_v1_api(self) -> None:
        registered_app = FastAPI()
        registered_app.include_router(api_router)
        paths = registered_app.openapi()["paths"]

        message_path = paths["/api/v1/projects/{project_id}/messages"]
        self.assertIn("post", message_path)
        self.assertIn("get", message_path)


if __name__ == "__main__":
    unittest.main()
