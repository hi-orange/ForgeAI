"""Opt-in real MySQL regression, always in a newly created disposable database.

FORGEAI_TEST_MYSQL=1 uses the configured server credentials, never its database.
"""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import test_durable_workflow as fixtures
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.core.settings import settings
from app.db.database import Base
from app.models.project import Project
from app.models.project_message import ProjectMessage
from app.models.task_execution import TaskExecution
from app.models.user import User
from app.models.workflow_event import WorkflowEvent
from app.services.task_execution import save_execution_draft, utc_now
from app.services.workflow_jobs import recover_orphaned_work


@unittest.skipUnless(
    os.environ.get("FORGEAI_TEST_MYSQL") == "1", "requires isolated MySQL test database"
)
class MySqlRecoveryConcurrencyTests(unittest.TestCase):
    _execution_graph = fixtures.DurableWorkflowTests._execution_graph

    def setUp(self):
        self.database = "forgeai_test_" + uuid4().hex
        self.admin = create_engine(settings.database_url, pool_pre_ping=True)
        if self.admin.dialect.name != "mysql":
            self.skipTest("requires MySQL")
        with self.admin.begin() as conn:
            conn.execute(text(f"CREATE DATABASE `{self.database}` CHARACTER SET utf8mb4"))
        self.addCleanup(self._drop_database)
        self.engine = create_engine(self.admin.url.set(database=self.database))
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions() as db:
            user = User(
                username="regression", email="regression@example.test", hashed_password="unused"
            )
            db.add(user)
            db.flush()
            project = Project(user_id=user.id, name="isolated test", status="draft")
            db.add(project)
            db.flush()
            message = ProjectMessage(
                project_id=project.id,
                sequence=1,
                sender="user",
                content="test",
                client_message_id="test",
            )
            db.add(message)
            db.commit()
            self.user_id, self.project_id, self.message_id = user.id, project.id, message.id
            _, _, execution = self._execution_graph(db, "concurrency")
            self.execution_id = execution.execution_id

    def _drop_database(self):
        # Only the database this test successfully created; never a supplied identifier.
        with self.admin.begin() as conn:
            conn.execute(text(f"DROP DATABASE `{self.database}`"))
        self.admin.dispose()

    def _recover(self):
        with self.sessions() as db:
            db.execute(text("SET SESSION innodb_lock_wait_timeout=2"))
            return recover_orphaned_work(db)

    def test_scan_does_not_lock_project_or_wait_for_healthy_checkpoint_writer(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.sessions() as writer:
                execution = writer.scalar(
                    select(TaskExecution)
                    .where(
                        TaskExecution.execution_id == self.execution_id,
                    )
                    .with_for_update()
                )
                # Old joined FOR UPDATE waits on this execution while holding Project X.
                self.assertEqual(pool.submit(self._recover).result(timeout=5), 0)
                save_execution_draft(
                    writer,
                    execution,
                    {
                        "checkpoint": {
                            "activity": [
                                {"id": "read", "name": "read_file", "label": "read", "ok": True},
                            ]
                        }
                    },
                )
            with self.sessions() as db:
                self.assertIsNotNone(
                    db.scalar(
                        select(WorkflowEvent).where(
                            WorkflowEvent.execution_id == self.execution_id,
                            WorkflowEvent.event_type == "activity",
                        )
                    )
                )

    def test_expired_locked_candidate_is_skipped_then_recovered_exactly_once(self):
        with self.sessions() as db:
            row = db.get(TaskExecution, self.execution_id)
            row.expires_at = utc_now() - timedelta(seconds=2)
            db.commit()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.sessions() as writer:
                writer.scalar(
                    select(TaskExecution)
                    .where(
                        TaskExecution.execution_id == self.execution_id,
                    )
                    .with_for_update()
                )
                self.assertEqual(pool.submit(self._recover).result(timeout=5), 0)
        self.assertEqual(self._recover(), 1)
        self.assertEqual(self._recover(), 0)
