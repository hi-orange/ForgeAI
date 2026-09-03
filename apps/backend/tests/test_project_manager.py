import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
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

from app.agents import project_manager as project_manager_agent
from app.agents.prompts.project_manager import MESSAGE_CLASSIFICATION_PROMPT_VERSION
from app.api.deps import get_current_user
from app.api.v1.project_messages import router
from app.core.exceptions import BusinessException, register_exception_handlers
from app.core.settings import settings
from app.db.database import Base, get_db
from app.models.build_run import BuildRun
from app.models.project import Project, ProjectStatus
from app.models.project_message import ProjectMessage, ProjectMessageSender
from app.models.project_message_classification import (
    ProjectMessageCategory,
    ProjectMessageClassification,
)
from app.models.user import User
from app.schemas.project_message_classification import ProjectMessageClassificationDecision
from app.services import project_manager as project_manager_service


class ProjectManagerClassificationTests(unittest.TestCase):
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
                name="Shop",
                prompt="Build a shop",
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

    def _add_message(
        self,
        *,
        project_id: int,
        sequence: int,
        sender: ProjectMessageSender,
        content: str,
    ) -> ProjectMessage:
        with self.session_factory() as db:
            message = ProjectMessage(
                project_id=project_id,
                sequence=sequence,
                sender=sender.value,
                content=content,
                client_message_id=f"message-{project_id}-{sequence}",
            )
            db.add(message)
            db.commit()
            return message

    def _classification_path(self, message_id: int, *, project_id: int | None = None) -> str:
        return (
            f"/api/v1/projects/{project_id or self.project.id}/messages/{message_id}/classification"
        )

    def test_classifies_and_persists_message_without_starting_build(self) -> None:
        first = self._add_message(
            project_id=self.project.id,
            sequence=1,
            sender=ProjectMessageSender.USER,
            content="Create an online shop",
        )
        assistant = self._add_message(
            project_id=self.project.id,
            sequence=2,
            sender=ProjectMessageSender.ASSISTANT,
            content="The first version is ready",
        )
        target = self._add_message(
            project_id=self.project.id,
            sequence=3,
            sender=ProjectMessageSender.USER,
            content="Add a refund flow",
        )
        self._add_message(
            project_id=self.project.id,
            sequence=4,
            sender=ProjectMessageSender.USER,
            content="This future message must not be context",
        )
        decision = ProjectMessageClassificationDecision(
            category=ProjectMessageCategory.PRODUCT_CHANGE,
            decision_summary="新增退款这一用户可见行为。",
        )

        with patch.object(
            project_manager_service.project_manager_agent,
            "classify_message",
            return_value=decision,
        ) as classify:
            response = self.client.post(self._classification_path(target.id))

        self.assertEqual(response.status_code, 200)
        result = response.json()["data"]
        self.assertEqual(result["message_id"], target.id)
        self.assertEqual(result["category"], ProjectMessageCategory.PRODUCT_CHANGE.value)
        self.assertEqual(result["decision_summary"], "新增退款这一用户可见行为。")
        self.assertEqual(result["classifier_model"], settings.deepseek_model)
        self.assertEqual(result["prompt_version"], MESSAGE_CLASSIFICATION_PROMPT_VERSION)

        call = classify.call_args.kwargs
        self.assertEqual(call["project_name"], self.project.name)
        self.assertEqual(call["project_status"], self.project.status)
        self.assertEqual(
            [message["sequence"] for message in call["recent_messages"]],
            [first.sequence, assistant.sequence],
        )
        self.assertEqual(call["message_sequence"], target.sequence)
        self.assertEqual(call["message_content"], target.content)

        with self.session_factory() as db:
            stored = db.scalar(
                select(ProjectMessageClassification).where(
                    ProjectMessageClassification.message_id == target.id
                )
            )
            self.assertIsNotNone(stored)
            self.assertEqual(stored.category, ProjectMessageCategory.PRODUCT_CHANGE.value)
            self.assertEqual(db.scalar(select(func.count()).select_from(BuildRun)), 0)

        get_response = self.client.get(self._classification_path(target.id))
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json()["data"], result)

    def test_repeated_classification_reuses_persisted_result(self) -> None:
        message = self._add_message(
            project_id=self.project.id,
            sequence=1,
            sender=ProjectMessageSender.USER,
            content="How is the build going?",
        )
        decision = ProjectMessageClassificationDecision(
            category=ProjectMessageCategory.INQUIRY,
            decision_summary="用户只是在询问进度。",
        )

        with patch.object(
            project_manager_service.project_manager_agent,
            "classify_message",
            return_value=decision,
        ) as classify:
            first = self.client.post(self._classification_path(message.id))
            replay = self.client.post(self._classification_path(message.id))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["data"], first.json()["data"])
        classify.assert_called_once()

    def test_unclassified_and_unknown_messages_return_not_found(self) -> None:
        message = self._add_message(
            project_id=self.project.id,
            sequence=1,
            sender=ProjectMessageSender.USER,
            content="Hello",
        )

        unclassified = self.client.get(self._classification_path(message.id))
        unknown = self.client.post(self._classification_path(999999))

        self.assertEqual(unclassified.status_code, 404)
        self.assertEqual(unclassified.json()["msg"], "消息尚未分类")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["msg"], "项目消息不存在")

    def test_only_user_messages_can_be_classified(self) -> None:
        message = self._add_message(
            project_id=self.project.id,
            sequence=1,
            sender=ProjectMessageSender.ASSISTANT,
            content="A system reply",
        )

        response = self.client.post(self._classification_path(message.id))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "只能分类用户消息")

    def test_project_ownership_and_message_scope_are_enforced(self) -> None:
        own_message = self._add_message(
            project_id=self.project.id,
            sequence=1,
            sender=ProjectMessageSender.USER,
            content="Owner message",
        )
        other_message = self._add_message(
            project_id=self.other_project.id,
            sequence=1,
            sender=ProjectMessageSender.USER,
            content="Other message",
        )

        foreign_project = self.client.post(
            self._classification_path(other_message.id, project_id=self.other_project.id)
        )
        cross_project_message = self.client.post(self._classification_path(other_message.id))
        self.current_user = self.other_user
        hidden_owner_message = self.client.get(self._classification_path(own_message.id))

        self.assertEqual(foreign_project.status_code, 404)
        self.assertEqual(foreign_project.json()["msg"], "项目不存在")
        self.assertEqual(cross_project_message.status_code, 404)
        self.assertEqual(cross_project_message.json()["msg"], "项目消息不存在")
        self.assertEqual(hidden_owner_message.status_code, 404)
        self.assertEqual(hidden_owner_message.json()["msg"], "项目不存在")

    def test_classification_cascades_when_message_is_deleted(self) -> None:
        message = self._add_message(
            project_id=self.project.id,
            sequence=1,
            sender=ProjectMessageSender.USER,
            content="Stop the build",
        )
        with self.session_factory() as db:
            classification = ProjectMessageClassification(
                message_id=message.id,
                category=ProjectMessageCategory.STOP.value,
                decision_summary="用户明确要求停止。",
                classifier_model="test-model",
                prompt_version=MESSAGE_CLASSIFICATION_PROMPT_VERSION,
            )
            db.add(classification)
            db.commit()
            classification_id = classification.id

            stored_message = db.get(ProjectMessage, message.id)
            self.assertIsNotNone(stored_message)
            db.delete(stored_message)
            db.commit()

        with self.session_factory() as db:
            self.assertIsNone(db.get(ProjectMessageClassification, classification_id))

    def test_database_allows_only_one_valid_classification_per_message(self) -> None:
        message = self._add_message(
            project_id=self.project.id,
            sequence=1,
            sender=ProjectMessageSender.USER,
            content="Stop",
        )
        with self.session_factory() as db:
            db.add(
                ProjectMessageClassification(
                    message_id=message.id,
                    category=ProjectMessageCategory.STOP.value,
                    decision_summary="Stop requested",
                    classifier_model="test-model",
                    prompt_version=MESSAGE_CLASSIFICATION_PROMPT_VERSION,
                )
            )
            db.commit()

            db.add(
                ProjectMessageClassification(
                    message_id=message.id,
                    category=ProjectMessageCategory.INQUIRY.value,
                    decision_summary="Duplicate",
                    classifier_model="test-model",
                    prompt_version=MESSAGE_CLASSIFICATION_PROMPT_VERSION,
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

        invalid_category_message = self._add_message(
            project_id=self.project.id,
            sequence=2,
            sender=ProjectMessageSender.USER,
            content="Invalid category target",
        )
        with self.session_factory() as db:
            db.add(
                ProjectMessageClassification(
                    message_id=invalid_category_message.id,
                    category="unsupported",
                    decision_summary="Invalid",
                    classifier_model="test-model",
                    prompt_version=MESSAGE_CLASSIFICATION_PROMPT_VERSION,
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

    def test_concurrent_insert_returns_the_winning_classification(self) -> None:
        db = MagicMock(spec=Session)
        message = ProjectMessage(
            id=12,
            project_id=self.project.id,
            sequence=1,
            sender=ProjectMessageSender.USER.value,
            content="Stop",
            client_message_id="race-message",
        )
        winning = ProjectMessageClassification(
            id=5,
            message_id=message.id,
            category=ProjectMessageCategory.STOP.value,
            decision_summary="Winning decision",
            classifier_model="test-model",
            prompt_version=MESSAGE_CLASSIFICATION_PROMPT_VERSION,
        )
        decision = ProjectMessageClassificationDecision(
            category=ProjectMessageCategory.STOP,
            decision_summary="Concurrent decision",
        )
        db.commit.side_effect = IntegrityError("insert", {}, Exception("duplicate"))

        with (
            patch.object(
                project_manager_service.project_service,
                "get_user_project",
                return_value=self.project,
            ),
            patch.object(project_manager_service, "_get_project_message", return_value=message),
            patch.object(
                project_manager_service,
                "_get_classification",
                side_effect=(None, winning),
            ),
            patch.object(project_manager_service, "_recent_context", return_value=[]),
            patch.object(
                project_manager_service.project_manager_agent,
                "classify_message",
                return_value=decision,
            ),
        ):
            result = project_manager_service.classify_user_message(
                db,
                self.user,
                self.project.id,
                message.id,
            )

        self.assertIs(result, winning)
        db.rollback.assert_called_once_with()

    def test_agent_parses_strict_json_and_uses_structured_input(self) -> None:
        raw = (
            "```json\n"
            '{"category":"implementation_repair",'
            '"decision_summary":"现有登录行为没有正常工作。"}\n'
            "```"
        )
        with patch.object(project_manager_agent, "chat_completion", return_value=raw) as chat:
            decision = project_manager_agent.classify_message(
                project_name="Demo",
                project_status=ProjectStatus.AVAILABLE.value,
                recent_messages=[{"sequence": 1, "sender": "user", "content": "Login is required"}],
                message_sequence=2,
                message_content="The login button does nothing",
            )

        self.assertEqual(decision.category, ProjectMessageCategory.IMPLEMENTATION_REPAIR)
        self.assertEqual(decision.decision_summary, "现有登录行为没有正常工作。")
        request = chat.call_args.kwargs
        self.assertEqual(request["temperature"], 0.0)
        self.assertEqual(request["max_tokens"], 256)
        self.assertTrue(request["json_output"])
        payload = json.loads(request["messages"][1]["content"])
        self.assertEqual(payload["message_to_classify"]["sequence"], 2)
        self.assertEqual(payload["recent_messages_before_target"][0]["sequence"], 1)

    def test_agent_rejects_malformed_or_unknown_decisions(self) -> None:
        invalid_responses = (
            "not json",
            '{"category":"unknown","decision_summary":"invalid"}',
            '{"category":"stop","decision_summary":"ok","extra":true}',
            '{"category":"stop","decision_summary":"   "}',
        )
        for raw in invalid_responses:
            with (
                self.subTest(raw=raw),
                patch.object(project_manager_agent, "chat_completion", return_value=raw),
                self.assertRaisesRegex(BusinessException, "ProjectManager 消息分类返回格式异常"),
            ):
                project_manager_agent.classify_message(
                    project_name="Demo",
                    project_status=ProjectStatus.DRAFT.value,
                    recent_messages=[],
                    message_sequence=1,
                    message_content="Hello",
                )

    def test_classification_routes_are_registered(self) -> None:
        registered_app = FastAPI()
        registered_app.include_router(router, prefix="/api/v1")
        paths = registered_app.openapi()["paths"]

        classification_path = "/api/v1/projects/{project_id}/messages/{message_id}/classification"
        self.assertIn("post", paths[classification_path])
        self.assertIn("get", paths[classification_path])

    def test_migration_upgrade_and_downgrade(self) -> None:
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "2a6c9e4f1b7d_create_project_message_classification.py"
        )
        spec = importlib.util.spec_from_file_location(
            "message_classification_migration",
            migration_path,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        legacy_engine = create_engine("sqlite+pysqlite://")
        metadata = MetaData()
        Table(
            "project_message",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("sender", String(length=16), nullable=False),
            Column("content", Text, nullable=False),
        )
        metadata.create_all(legacy_engine)

        with legacy_engine.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            self.assertIn(
                "project_message_classification",
                inspect(connection).get_table_names(),
            )
            migration.downgrade()
            self.assertNotIn(
                "project_message_classification",
                inspect(connection).get_table_names(),
            )

        legacy_engine.dispose()


if __name__ == "__main__":
    unittest.main()
