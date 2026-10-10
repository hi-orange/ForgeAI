import unittest
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.models.agent_usage_event import AgentUsageEvent
from app.models.build_run import BuildRun
from app.models.plan import Plan
from app.models.project import Project
from app.models.project_message import ProjectMessage
from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.models.user import User
from app.schemas.leader import NextAction, NextActionKind
from app.services import decision_event, deployment, evaluation, run_control


class PlatformCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions() as db:
            self.user = User(username="owner", email="owner@example.com", hashed_password="unused")
            db.add(self.user)
            db.flush()
            self.project = Project(
                user_id=self.user.id,
                name="Demo",
                prompt="Build a demo",
                status="draft",
            )
            db.add(self.project)
            db.flush()
            self.message = ProjectMessage(
                project_id=self.project.id,
                sequence=1,
                sender="user",
                content="build it",
                client_message_id="m1",
            )
            db.add(self.message)
            db.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    def _running_graph(self, db):
        run = BuildRun(
            project_id=self.project.id,
            run_id="run_active",
            status="running",
            stage="developer",
            active_slot=1,
        )
        plan = Plan(
            plan_id="plan_active",
            project_id=self.project.id,
            build_run_id=run.run_id,
            version=1,
            cause_message_id=self.message.id,
            definition_hash="a" * 64,
            status="running",
        )
        task = Task(
            task_id="task_active",
            plan_id=plan.plan_id,
            task_key="engineering_delivery",
            position=1,
            recipient="Code Engineer",
            title="Implement",
            instructions="Implement the approved behavior",
            expected_output_type="code",
            input_configuration_item_ids=[],
            depends_on_task_ids=[],
            status="running",
        )
        execution = TaskExecution(
            execution_id="exec_active",
            task_id=task.task_id,
            attempt=1,
            status="running",
            active_slot=1,
            started_at=datetime.now(UTC).replace(tzinfo=None),
            expires_at=(datetime.now(UTC) + timedelta(minutes=5)).replace(tzinfo=None),
        )
        db.add_all((run, plan, task, execution))
        db.commit()
        return run, plan, task, execution

    def test_cancel_fences_run_plan_task_and_execution_atomically(self) -> None:
        with self.sessions() as db:
            self._running_graph(db)
            cancelled = run_control.cancel_active_run(db, self.user, self.project.id, "run_active")
            self.assertEqual(cancelled.status, "cancelled")
            self.assertIsNone(cancelled.active_slot)
            plan = db.scalar(select(Plan).where(Plan.plan_id == "plan_active"))
            task = db.scalar(select(Task).where(Task.task_id == "task_active"))
            self.assertEqual(plan.status, "cancelled")
            self.assertEqual(task.status, "cancelled")
            execution = db.get(TaskExecution, "exec_active")
            self.assertEqual(execution.status, "cancelled")
            self.assertIsNone(execution.active_slot)
            self.assertIsNotNone(execution.finished_at)

    def test_decision_is_replayed_and_keeps_applied_evidence(self) -> None:
        with self.sessions() as db:
            action = NextAction(
                action=NextActionKind.REPLY,
                reason_code="inquiry_no_delivery",
                summary="No delivery",
                reply_text="Tell me what to build.",
            )
            saved = decision_event.record_decision(
                db,
                decision_key=f"message:{self.message.id}",
                project_id=self.project.id,
                build_run_id=None,
                message_id=self.message.id,
                category="inquiry",
                action=action,
            )
            replay = decision_event.record_decision(
                db,
                decision_key=f"message:{self.message.id}",
                project_id=self.project.id,
                build_run_id=None,
                message_id=self.message.id,
                category="inquiry",
                action=action,
            )
            self.assertEqual(replay, saved)
            decision_event.mark_applied(db, f"message:{self.message.id}")
            rows = decision_event.list_project_decisions(db, self.project.id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, "applied")

    def test_eval_uses_measured_usage_and_never_invents_accuracy(self) -> None:
        with self.sessions() as db:
            db.add(
                AgentUsageEvent(
                    event_id="use_1",
                    project_id=self.project.id,
                    role="Leader",
                    model="test-model",
                    protocol="native",
                    prompt_tokens=100,
                    completion_tokens=20,
                    total_tokens=120,
                    duration_ms=250,
                )
            )
            db.commit()
            report = evaluation.evaluate_project(db, self.user, self.project.id)
            self.assertEqual(report.usage.total_tokens, 120)
            self.assertEqual(report.usage.duration_ms, 250)
            metrics = {metric.name: metric for metric in report.metrics}
            self.assertEqual(metrics["routing_accuracy"].status, "unavailable")
            self.assertEqual(metrics["message_classification_accuracy"].status, "unavailable")

    def test_publish_creates_durable_attempt_and_external_secret_file(self) -> None:
        with self.sessions() as db, TemporaryDirectory() as runtime_root:
            release = BuildRun(
                project_id=self.project.id,
                run_id="run_release",
                status="running",
                stage="qa",
                active_slot=1,
            )
            db.add(release)
            db.flush()
            release.status = "succeeded"
            release.active_slot = None
            db.commit()
            worker = MagicMock()
            with (
                patch.object(deployment.settings, "runtime_data_root", runtime_root),
                patch.object(deployment, "workspace_is_ready", return_value=True),
                patch.object(deployment, "_docker_binary", return_value="docker"),
                patch.object(deployment, "_assert_trusted_deployment_files"),
                patch.object(deployment, "_allocate_port", return_value=18400),
                patch.object(
                    deployment,
                    "get_requirements_status",
                    return_value=SimpleNamespace(state="completed", run_id="run_release"),
                ),
                patch.object(deployment.threading, "Thread", return_value=worker),
            ):
                created = deployment.start_deployment(db, self.user, self.project.id)
                secret_file = deployment._env_path(created.deployment_id)
                self.assertEqual(created.status, "queued")
                self.assertEqual(created.secret_reference, f"local-file:{created.deployment_id}")
                self.assertTrue(secret_file.is_file())
                self.assertIn("DATABASE_URL=postgresql+psycopg://", secret_file.read_text())
                self.assertNotIn("POSTGRES_PASSWORD", created.secret_reference)
                worker.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
