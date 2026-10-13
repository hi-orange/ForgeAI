"""Progress, events and execution fences are persisted in a real database."""

from datetime import timedelta

from sqlalchemy import select, update
from test_product_manager_workflow import ProductManagerWorkflowFixture

from app.core.exceptions import ConflictException
from app.models.task_execution import TaskExecution
from app.models.workflow_event import WorkflowEvent
from app.orchestration import test_engineer as orch
from app.services import task as task_service
from app.services.task_execution import start_execution, utc_now


class QualityActivityPersistenceTests(ProductManagerWorkflowFixture):
    def setUp(self):
        super().setUp()
        _, task = self._create_plan()
        with self.session_factory() as db:
            task_service.claim_product_manager_task(
                db, self.owner, self.project.id, self.run.run_id, task.task_id
            )
            execution = start_execution(
                db, self.owner, self.project.id, self.run.run_id, task.task_id
            )
            self.execution_id = execution.execution_id

    def test_activity_and_progress_persist_without_duplicate_events(self):
        with self.session_factory() as db:
            execution = db.get(TaskExecution, self.execution_id)
            old_expiry = execution.expires_at
            for name in ("quality_start", "quality_error"):
                orch._record_quality_activity(
                    db, execution, name=name, label=name, detail=name, ok=name != "quality_error"
                )
            orch._save_quality_progress(db, execution, {"schema_version": 2, "checks": {}})
        with self.session_factory() as db:
            execution = db.get(TaskExecution, self.execution_id)
            checkpoint = execution.draft["checkpoint"]
            self.assertEqual(
                [a["name"] for a in checkpoint["activity"]], ["quality_start", "quality_error"]
            )
            self.assertFalse(checkpoint["activity"][-1]["ok"])
            self.assertEqual(checkpoint["verification"], {"schema_version": 2, "checks": {}})
            self.assertGreater(execution.expires_at, old_expiry)
            events = list(
                db.scalars(
                    select(WorkflowEvent).where(
                        WorkflowEvent.execution_id == self.execution_id,
                        WorkflowEvent.event_type == "activity",
                    )
                )
            )
            self.assertEqual(len(events), 2)

    def test_stale_execution_cannot_write_after_cancellation_or_expiry(self):
        for change in (
            {"status": "cancelled", "active_slot": None},
            {"status": "running", "active_slot": 1, "expires_at": utc_now() - timedelta(seconds=1)},
        ):
            with self.subTest(change=change), self.session_factory() as stale:
                execution = stale.get(TaskExecution, self.execution_id)
                stale.commit()
                with self.session_factory() as owner:
                    owner.execute(
                        update(TaskExecution)
                        .where(TaskExecution.execution_id == self.execution_id)
                        .values(**change)
                    )
                    owner.commit()
                with self.assertRaises(ConflictException):
                    orch._save_quality_progress(stale, execution, {"unauthorized": True})
                stale.commit()
                stale.refresh(execution)
                self.assertIsNone(execution.draft)
                self.assertEqual(list(stale.scalars(select(WorkflowEvent))), [])
