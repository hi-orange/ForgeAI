import time
import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.models.build_run import BuildRun
from app.models.plan import Plan
from app.models.project import Project
from app.models.project_message import ProjectMessage
from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.models.user import User
from app.models.workflow_event import WorkflowEvent
from app.models.workflow_job import WorkflowJob
from app.schemas.leader import NextAction, NextActionKind
from app.services import decision_event
from app.services.task_execution import utc_now
from app.services.workflow_events import sync_checkpoint_activity_events
from app.services.workflow_jobs import (
    cancel_jobs_for_run,
    claim_next_job,
    enqueue_job,
    fail_job,
    recover_expired_jobs,
    recover_orphaned_work,
    recover_unapplied_decisions,
)
from app.worker import DurableWorker


class DurableWorkflowTests(unittest.TestCase):
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
            user = User(
                username="durable-owner",
                email="durable@example.com",
                hashed_password="unused",
            )
            db.add(user)
            db.flush()
            project = Project(
                user_id=user.id,
                name="Durable app",
                prompt="Build it",
                status="draft",
            )
            db.add(project)
            db.flush()
            message = ProjectMessage(
                project_id=project.id,
                sequence=1,
                sender="user",
                content="build it",
                client_message_id="durable-message",
            )
            db.add(message)
            db.commit()
            self.user_id = user.id
            self.project_id = project.id
            self.message_id = message.id

    def tearDown(self) -> None:
        self.engine.dispose()

    def _execution_graph(
        self,
        db,
        suffix: str,
        *,
        expired: bool = False,
        activity: list[dict[str, object]] | None = None,
        recipient: str = "Code Engineer",
    ) -> tuple[BuildRun, Task, TaskExecution]:
        now = utc_now()
        run = BuildRun(
            project_id=self.project_id,
            run_id=f"run_{suffix}",
            status="running",
            stage="developer",
            active_slot=1,
        )
        db.add(run)
        db.flush()
        plan = Plan(
            plan_id=f"plan_{suffix}",
            project_id=self.project_id,
            build_run_id=run.run_id,
            version=1,
            cause_message_id=self.message_id,
            definition_hash=(suffix[0] * 64),
            status="running",
        )
        db.add(plan)
        db.flush()
        task = Task(
            task_id=f"task_{suffix}",
            plan_id=plan.plan_id,
            task_key=("requirements" if recipient == "Product Manager" else "engineering_delivery"),
            position=1,
            recipient=recipient,
            title="Implement",
            instructions="Implement the approved behavior",
            expected_output_type="app_spec" if recipient == "Product Manager" else "code",
            input_configuration_item_ids=[],
            depends_on_task_ids=[],
            status="running",
        )
        db.add(task)
        db.flush()
        execution = TaskExecution(
            execution_id=f"exec_{suffix}",
            task_id=task.task_id,
            attempt=1,
            status="running",
            active_slot=1,
            started_at=now,
            expires_at=now - timedelta(seconds=1) if expired else now + timedelta(minutes=10),
            draft={
                "checkpoint": {
                    "kind": "code_engineer_checkpoint",
                    "activity": list(activity or []),
                }
            },
        )
        db.add(execution)
        db.commit()
        return run, task, execution

    def test_enqueue_is_idempotent_and_claim_is_fenced(self) -> None:
        with self.sessions() as db:
            run, task, execution = self._execution_graph(db, "claim")
            first = enqueue_job(
                db,
                job_type="engineering",
                idempotency_key="engineering:exec_claim",
                project_id=self.project_id,
                build_run_id=run.run_id,
                task_id=task.task_id,
                execution_id=execution.execution_id,
                payload={"user_id": self.user_id},
            )
            replay = enqueue_job(
                db,
                job_type="engineering",
                idempotency_key="engineering:exec_claim",
                project_id=self.project_id,
                build_run_id=run.run_id,
                task_id=task.task_id,
                execution_id=execution.execution_id,
                payload={"user_id": self.user_id},
            )
            self.assertEqual(replay.job_id, first.job_id)
            claimed = claim_next_job(db, "worker-a")
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed.status, "running")
            self.assertEqual(claimed.attempt, 1)
            self.assertIsNone(claim_next_job(db, "worker-b"))
            event_types = list(
                db.scalars(
                    select(WorkflowEvent.event_type)
                    .where(WorkflowEvent.job_id == first.job_id)
                    .order_by(WorkflowEvent.sequence)
                )
            )
            self.assertEqual(event_types, ["job_queued", "job_claimed"])

    def test_worker_claims_executes_and_completes_persisted_job(self) -> None:
        with self.sessions() as db:
            run, task, execution = self._execution_graph(db, "worker")
            job = enqueue_job(
                db,
                job_type="engineering",
                idempotency_key="engineering:exec_worker",
                project_id=self.project_id,
                build_run_id=run.run_id,
                task_id=task.task_id,
                execution_id=execution.execution_id,
                payload={"user_id": self.user_id},
            )
        worker = DurableWorker(worker_id="test-worker")
        worker._last_recovery = time.monotonic()
        with (
            patch("app.worker.SessionLocal", self.sessions),
            patch("app.worker.execute_job") as execute,
        ):
            self.assertTrue(worker.run_once())
        execute.assert_called_once_with(job.job_id, "test-worker")
        with self.sessions() as db:
            completed = db.get(WorkflowJob, job.job_id)
            self.assertIsNotNone(completed)
            assert completed is not None
            self.assertEqual(completed.status, "succeeded")
            self.assertIsNone(completed.lease_owner)

    def test_cancelling_run_jobs_appends_cancellation_fact(self) -> None:
        with self.sessions() as db:
            run, task, execution = self._execution_graph(db, "cancel")
            job = enqueue_job(
                db,
                job_type="engineering",
                idempotency_key="engineering:exec_cancel",
                project_id=self.project_id,
                build_run_id=run.run_id,
                task_id=task.task_id,
                execution_id=execution.execution_id,
                payload={"user_id": self.user_id},
            )
            self.assertEqual(cancel_jobs_for_run(db, run.run_id, commit=True), 1)
            db.refresh(job)
            self.assertEqual(job.status, "cancelled")
            cancelled = db.scalar(
                select(WorkflowEvent).where(
                    WorkflowEvent.job_id == job.job_id,
                    WorkflowEvent.event_type == "job_cancelled",
                )
            )
            self.assertIsNotNone(cancelled)

    def test_failed_job_fences_execution_before_retry(self) -> None:
        with self.sessions() as db:
            run, task, execution = self._execution_graph(
                db,
                "retry",
                activity=[{"id": "read_1", "name": "read", "label": "读取源码"}],
            )
            job = enqueue_job(
                db,
                job_type="engineering",
                idempotency_key="engineering:exec_retry",
                project_id=self.project_id,
                build_run_id=run.run_id,
                task_id=task.task_id,
                execution_id=execution.execution_id,
                payload={"user_id": self.user_id},
            )
            claim_next_job(db, "worker-a")
            fail_job(db, job.job_id, "worker-a", RuntimeError("container stopped"))
            db.refresh(job)
            db.refresh(execution)
            self.assertEqual(job.status, "retry_wait")
            self.assertEqual(job.payload["recovery_execution_id"], execution.execution_id)
            self.assertEqual(execution.status, "failed")
            self.assertIsNone(execution.active_slot)
            self.assertEqual(
                db.scalar(
                    select(WorkflowEvent.event_type).where(
                        WorkflowEvent.execution_id == execution.execution_id,
                        WorkflowEvent.event_type == "activity",
                    )
                ),
                "activity",
            )

    def test_expired_job_becomes_claimable_recovery(self) -> None:
        with self.sessions() as db:
            run, task, execution = self._execution_graph(db, "lease")
            job = enqueue_job(
                db,
                job_type="engineering",
                idempotency_key="engineering:exec_lease",
                project_id=self.project_id,
                build_run_id=run.run_id,
                task_id=task.task_id,
                execution_id=execution.execution_id,
                payload={"user_id": self.user_id},
            )
            claim_next_job(db, "dead-worker")
            job.lease_expires_at = utc_now() - timedelta(seconds=1)
            db.commit()
            self.assertEqual(recover_expired_jobs(db), 1)
            db.refresh(job)
            db.refresh(execution)
            self.assertEqual(job.status, "retry_wait")
            self.assertEqual(execution.status, "failed")
            recovered = claim_next_job(db, "recovery-worker")
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.job_id, job.job_id)
            self.assertEqual(recovered.attempt, 2)

    def test_recovery_scan_enqueues_orphaned_execution(self) -> None:
        with self.sessions() as db:
            run, task, execution = self._execution_graph(db, "orphan", expired=True)
            self.assertEqual(recover_orphaned_work(db), 1)
            db.refresh(execution)
            self.assertEqual(execution.status, "failed")
            job = db.scalar(select(WorkflowJob).where(WorkflowJob.task_id == task.task_id))
            self.assertIsNotNone(job)
            assert job is not None
            self.assertEqual(job.status, "queued")
            self.assertEqual(job.build_run_id, run.run_id)
            self.assertEqual(job.payload["recovery_execution_id"], execution.execution_id)

    def test_recovery_scan_covers_product_manager_execution(self) -> None:
        with self.sessions() as db:
            _, task, execution = self._execution_graph(
                db,
                "product",
                expired=True,
                recipient="Product Manager",
            )
            self.assertEqual(recover_orphaned_work(db), 1)
            job = db.scalar(select(WorkflowJob).where(WorkflowJob.task_id == task.task_id))
            self.assertIsNotNone(job)
            assert job is not None
            self.assertEqual(job.job_type, "product_manager")
            self.assertEqual(job.payload["message_id"], self.message_id)
            self.assertEqual(job.payload["recovery_execution_id"], execution.execution_id)

    def test_recovery_scan_enqueues_old_pending_task(self) -> None:
        with self.sessions() as db:
            _, task, execution = self._execution_graph(db, "pending")
            db.delete(execution)
            task.status = "pending"
            db.commit()
            with patch(
                "app.services.workflow_jobs.settings.workflow_orphan_grace_seconds",
                0,
            ):
                self.assertEqual(recover_orphaned_work(db), 1)
            job = db.scalar(select(WorkflowJob).where(WorkflowJob.task_id == task.task_id))
            self.assertIsNotNone(job)
            assert job is not None
            self.assertEqual(job.job_type, "engineering")
            self.assertEqual(job.status, "queued")

    def test_recovery_replays_unapplied_persisted_decision(self) -> None:
        with self.sessions() as db:
            run, _, _ = self._execution_graph(db, "decision")
            action = NextAction(
                action=NextActionKind.REPLY,
                reason_code="durable_reply",
                summary="Persisted reply",
                reply_text="Recovered response",
                decided_by="engine",
            )
            decision_event.record_decision(
                db,
                decision_key=f"message:{self.message_id}",
                project_id=self.project_id,
                build_run_id=run.run_id,
                message_id=self.message_id,
                category="inquiry",
                action=action,
            )
            with patch(
                "app.services.workflow_jobs.settings.workflow_orphan_grace_seconds",
                0,
            ):
                self.assertEqual(recover_unapplied_decisions(db), 1)
            saved = decision_event.get_decision(db, f"message:{self.message_id}")
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved[0].status, "applied")
            reply = db.scalar(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == self.project_id,
                    ProjectMessage.sender == "assistant",
                )
            )
            self.assertIsNotNone(reply)
            assert reply is not None
            self.assertEqual(reply.content, "Recovered response")

    def test_activity_stream_appends_without_rewriting_old_facts(self) -> None:
        with self.sessions() as db:
            _, _, execution = self._execution_graph(
                db,
                "events",
                activity=[{"id": "one", "name": "read", "label": "读取"}],
            )
            self.assertEqual(sync_checkpoint_activity_events(db, execution), 1)
            db.commit()
            first = db.scalar(
                select(WorkflowEvent).where(
                    WorkflowEvent.execution_id == execution.execution_id,
                    WorkflowEvent.event_type == "activity",
                )
            )
            self.assertIsNotNone(first)
            assert first is not None
            original_payload = dict(first.payload)
            draft = dict(execution.draft or {})
            checkpoint = dict(draft["checkpoint"])
            checkpoint["activity"] = [
                *checkpoint["activity"],
                {"id": "two", "name": "write", "label": "写入"},
            ]
            draft["checkpoint"] = checkpoint
            execution.draft = draft
            flag_modified(execution, "draft")
            self.assertEqual(sync_checkpoint_activity_events(db, execution), 1)
            db.commit()
            events = list(
                db.scalars(
                    select(WorkflowEvent)
                    .where(
                        WorkflowEvent.execution_id == execution.execution_id,
                        WorkflowEvent.event_type == "activity",
                    )
                    .order_by(WorkflowEvent.sequence)
                )
            )
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].payload, original_payload)
            self.assertEqual(events[1].payload["id"], "two")
            events[0].payload = {"id": "rewritten"}
            with self.assertRaisesRegex(ValueError, "追加式事实"):
                db.commit()


if __name__ == "__main__":
    unittest.main()
