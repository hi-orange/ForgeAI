"""Engineering claim freezes approved inputs without calling the model."""

from __future__ import annotations

import json

from sqlalchemy import select, update
from test_product_manager_workflow import (
    ProductManagerWorkflowFixture,
    approval_payload,
    valid_spec,
)

from app.core.exceptions import BusinessException, ConflictException
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.project_message import ProjectMessage
from app.models.task_execution import TaskExecution
from app.models.task_result import TaskResult
from app.schemas.app_spec import AppSpec
from app.schemas.requirements import RequirementsApproval
from app.services import product_manager, project_manager
from app.services import task as task_service
from app.services.engineering import (
    APPROVAL_VERSION,
    INPUT_SNAPSHOT_KIND,
    TOOL_STRATEGY_VERSION,
    approved_spec_digest,
    claim_software_engineer_task,
    read_frozen_input_snapshot,
)
from app.services.requirement_approval import approve_requirements
from app.services.requirements import get_requirements_status, pause_active_execution


class EngineeringClaimTests(ProductManagerWorkflowFixture):
    def _publish_only(self):
        _, task = self._create_plan()
        self.chat.return_value = json.dumps(valid_spec())
        with self.session_factory() as db:
            task_service.claim_product_manager_task(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                task.task_id,
            )
            draft = product_manager.generate_task_app_spec(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                task.task_id,
            )
            result = product_manager.complete_task_app_spec(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                task.task_id,
                draft,
            )
            db.execute(
                update(TaskResult)
                .where(TaskResult.task_id == task.task_id)
                .values(prompt_version=APPROVAL_VERSION)
            )
            db.commit()
            return result

    def _assign(self, item_id: str):
        with self.session_factory() as db:
            item = db.scalar(select(ConfigurationItem).where(ConfigurationItem.item_id == item_id))
            if item is not None:
                result = db.scalar(
                    select(TaskResult).where(TaskResult.configuration_item_id == item_id)
                )
                if result is None or result.prompt_version != APPROVAL_VERSION:
                    item_id = approve_requirements(
                        db,
                        self.owner,
                        self.project.id,
                        self.run.run_id,
                        item_id,
                        RequirementsApproval.model_validate(
                            approval_payload(
                                item.payload,
                                client_message_id=f"test-approval-{item_id}",
                            )
                        ),
                    )
            return project_manager.create_engineering_delivery_task(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                item_id,
            )

    def _status(self):
        with self.session_factory() as db:
            return get_requirements_status(db, self.owner, self.project.id)

    def _claim(self, task_id: str):
        with self.session_factory() as db:
            return claim_software_engineer_task(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                task_id,
            )

    def test_claim_freezes_approved_inputs_and_creates_execution(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        task, execution = self._claim(delivery.task_id)

        self.assertEqual(task.status, "running")
        snapshot = read_frozen_input_snapshot(execution)
        assert snapshot is not None
        self.assertEqual(snapshot["kind"], INPUT_SNAPSHOT_KIND)
        self.assertEqual(snapshot["approved_item_id"], published.item_id)
        self.assertEqual(snapshot["template_version"], "fullstack-v1")
        self.assertEqual(snapshot["tool_strategy_version"], TOOL_STRATEGY_VERSION)
        self.assertEqual(snapshot["base_revision_id"], None)
        self.assertEqual(snapshot["workspace_key"], self.run.run_id)
        self.assertEqual(
            snapshot["approved_spec_digest"],
            approved_spec_digest(AppSpec.model_validate(valid_spec())),
        )
        self.assertTrue(snapshot["acceptance_requirements"])
        self.assertEqual(
            snapshot["call_budget"],
            {"max_model_turns": 40, "max_tool_calls": 120, "max_repair_rounds": 8},
        )

        with self.session_factory() as db:
            run = db.get(BuildRun, self.run.id)
            plan = db.scalar(select(Plan).where(Plan.plan_id == task.plan_id))
            self.assertEqual((run.status, run.stage, run.active_slot), ("running", "developer", 1))
            self.assertEqual(plan.status, "running")
            execution_row = db.scalar(
                select(TaskExecution).where(TaskExecution.task_id == task.task_id)
            )
            self.assertEqual(execution_row.attempt, 1)

        progress = self._status()
        self.assertEqual(progress.state, "engineering_running")
        self.assertEqual(progress.execution_id, execution.execution_id)
        self.assertTrue(progress.workspace_ready)
        self.assertFalse(progress.code_ready)
        self.chat.assert_called_once()

    def test_replay_claim_returns_same_frozen_execution(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        first_task, first_exec = self._claim(delivery.task_id)
        second_task, second_exec = self._claim(delivery.task_id)

        self.assertEqual(first_task.task_id, second_task.task_id)
        self.assertEqual(first_exec.execution_id, second_exec.execution_id)
        self.assertEqual(
            read_frozen_input_snapshot(first_exec),
            read_frozen_input_snapshot(second_exec),
        )
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(TaskExecution)).execution_id, first_exec.execution_id)
            count = len(list(db.scalars(select(TaskExecution)).all()))
            self.assertEqual(count, 1)

        progress = self._status()
        self.assertEqual(progress.state, "engineering_running")
        self.assertEqual(progress.execution_id, first_exec.execution_id)

    def test_claim_rejects_product_manager_tasks(self):
        _, task = self._create_plan()
        with self.assertRaises(BusinessException):
            self._claim(task.task_id)

    def test_new_messages_do_not_change_frozen_approved_item(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        _, execution = self._claim(delivery.task_id)
        frozen = read_frozen_input_snapshot(execution)
        assert frozen is not None

        with self.session_factory() as db:
            db.add(
                ProjectMessage(
                    project_id=self.project.id,
                    sequence=99,
                    sender="user",
                    content="请改成完全不同的产品",
                    client_message_id="later-change",
                )
            )
            db.commit()

        _, replay = self._claim(delivery.task_id)
        self.assertEqual(read_frozen_input_snapshot(replay), frozen)
        self.assertEqual(frozen["approved_item_id"], published.item_id)

    def test_status_refresh_does_not_create_another_execution(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        _, execution = self._claim(delivery.task_id)
        before = self._status()
        after = self._status()
        self.assertEqual(before.state, "engineering_running")
        self.assertEqual(after.execution_id, before.execution_id)
        with self.session_factory() as db:
            self.assertEqual(len(list(db.scalars(select(TaskExecution)).all())), 1)
            self.assertEqual(
                db.get(TaskExecution, execution.execution_id).execution_id,
                after.execution_id,
            )

    def test_pause_then_explicit_recovery_starts_new_attempt(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        _, execution = self._claim(delivery.task_id)
        with self.session_factory() as db:
            paused = pause_active_execution(db, self.owner, self.project.id, self.run.run_id)
        self.assertEqual(paused.state, "retry_available")
        self.assertEqual(paused.execution_id, execution.execution_id)
        with self.assertRaises(ConflictException):
            self._claim(delivery.task_id)
        with self.session_factory() as db:
            _, recovered = claim_software_engineer_task(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                delivery.task_id,
                recovery_execution_id=execution.execution_id,
            )
        self.assertEqual(recovered.attempt, 2)
        self.assertEqual(recovered.status, "running")
        self.assertEqual(
            read_frozen_input_snapshot(recovered), read_frozen_input_snapshot(execution)
        )
        progress = self._status()
        self.assertEqual(progress.state, "engineering_running")
        self.assertEqual(progress.execution_id, recovered.execution_id)
