import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from test_product_manager_workflow import (
    ProductManagerWorkflowFixture,
    approval_payload,
    valid_spec,
)

from app.api.deps import get_current_user
from app.api.v1.router import api_router
from app.core.exceptions import (
    BusinessException,
    ConflictException,
    NotFoundException,
    register_exception_handlers,
)
from app.db.database import Base, get_db
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.project import Project
from app.models.project_message import ProjectMessage
from app.models.requirement_clarification import RequirementClarification
from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.models.task_result import TaskResult
from app.orchestration.product_manager import run_product_manager_workflow
from app.schemas.product_manager import ProductManagerResult
from app.schemas.project_message import ProjectMessageCreate
from app.schemas.requirements import RequirementsApproval
from app.services import product_manager, project_manager, task, task_execution
from app.services.requirement_approval import approve_requirements
from app.services.requirements import get_requirements_status


class RequirementsLoopTests(ProductManagerWorkflowFixture):
    def test_late_model_response_cannot_overwrite_recovered_execution(self):
        entered, release = Event(), Event()

        def generate(**_):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(timeout=20))
                return json.dumps(valid_spec(open_questions=["旧执行的问题"]))
            return json.dumps(valid_spec())

        self.chat.side_effect = generate
        with ThreadPoolExecutor(max_workers=1) as pool:
            old_work = pool.submit(self._run)
            try:
                self.assertTrue(entered.wait(timeout=10))
                progress = self._status()
                with self.session_factory() as db:
                    previous = db.get(TaskExecution, progress.execution_id)
                    previous.expires_at = task_execution.utc_now() - timedelta(seconds=1)
                    db.commit()
                recovered = self._run(recovery_execution_id=progress.execution_id)
            finally:
                release.set()
            with self.assertRaises(ConflictException):
                old_work.result(timeout=20)
        self.assertEqual(self._status().result, recovered)
        self.assertEqual(self._status().state, "awaiting_approval")
        self.assertEqual(self._counts()[3], 1)

    def test_answer_model_failure_resumes_same_followup_without_saving_answer_again(self):
        first = self._questions()
        followup = self._answer(first.configuration_item_id)
        self.chat.side_effect = RuntimeError("model unavailable")
        with self.assertRaises(RuntimeError):
            self._execute_plan(followup)
        counts = self._counts()
        progress = self._status()
        self.assertEqual(progress.message_id, followup.cause_message_id)
        self.assertEqual(progress.state, "retry_available")
        self.chat.side_effect = None
        self.chat.return_value = json.dumps(valid_spec())
        with self.session_factory() as db:
            result = run_product_manager_workflow(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                followup.cause_message_id,
                recovery_execution_id=progress.execution_id,
            )
        self.assertEqual(self._counts()[:3], counts[:3])
        self.assertEqual(self._counts()[3:5], (counts[3] + 1, counts[4] + 1))
        self.assertEqual(result.plan_id, followup.plan_id)
        self.assertEqual(result.outcome, "awaiting_approval")

    def test_invalid_or_invalidated_upstream_cannot_be_published(self):
        first = self._questions()
        followup = self._answer(first.configuration_item_id)

        def generate(**_):
            with self.session_factory() as db:
                original = db.scalar(
                    select(ConfigurationItem).where(
                        ConfigurationItem.item_id == first.configuration_item_id,
                    )
                )
                original.state = "unusable"
                original.unusable_reason = "test invalidation during model call"
                original.unusable_at = task_execution.utc_now()
                db.commit()
            return json.dumps(valid_spec())

        self.chat.side_effect = generate
        with self.assertRaises(ConflictException):
            self._execute_plan(followup)
        self.assertEqual(self._counts()[3:5], (1, 1))
        self.assertEqual(self._status().state, "retry_available")

    def test_result_with_wrong_upstream_id_is_rejected(self):
        first = self._questions()
        followup = self._answer(first.configuration_item_id)
        with self.session_factory() as db:
            pending = db.scalar(select(Task).where(Task.plan_id == followup.plan_id))
            task.claim_product_manager_task(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                pending.task_id,
            )
            execution = task_execution.start_execution(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                pending.task_id,
            )
            draft = product_manager.generate_task_app_spec(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                pending.task_id,
                execution_id=execution.execution_id,
            )
            draft.input_configuration_item_ids = []
            with self.assertRaises(BusinessException):
                product_manager.complete_task_app_spec(
                    db,
                    self.owner,
                    self.project.id,
                    self.run.run_id,
                    pending.task_id,
                    draft,
                )
        self.assertEqual(self._counts()[3:5], (1, 1))

    def _questions(self, questions=None):
        self.chat.return_value = json.dumps(valid_spec(open_questions=questions or ["导出格式？"]))
        return self._run()

    def _answer(self, item_id, content="CSV", key="answer-1", user=None):
        with self.session_factory() as db:
            return project_manager.create_clarification_plan(
                db,
                user or self.owner,
                self.project.id,
                self.run.run_id,
                item_id,
                ProjectMessageCreate(content=content, client_message_id=key),
            )

    def _execute_plan(self, plan):
        with self.session_factory() as db:
            return run_product_manager_workflow(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                plan.cause_message_id,
            )

    def _status(self):
        with self.session_factory() as db:
            return get_requirements_status(db, self.owner, self.project.id)

    def _counts(self):
        with self.session_factory() as db:
            return tuple(
                db.scalar(select(func.count()).select_from(model))
                for model in (
                    ProjectMessage,
                    Plan,
                    Task,
                    ConfigurationItem,
                    TaskResult,
                    RequirementClarification,
                )
            )

    def test_multiple_rounds_use_exact_old_spec_and_answer_and_keep_history(self):
        first = self._questions(["导出格式？", "是否需要提醒？"])
        self.assertEqual(self._status().state, "awaiting_approval")
        second_plan = self._answer(first.configuration_item_id)
        self.assertEqual(self._status().state, "pending")
        self.chat.return_value = json.dumps(valid_spec(open_questions=["是否需要提醒？"]))
        second = self._execute_plan(second_plan)
        sent = json.loads(self.chat.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(sent["previous_item_id"], first.configuration_item_id)
        self.assertEqual(
            sent["previous_app_spec"], valid_spec(open_questions=["导出格式？", "是否需要提醒？"])
        )
        self.assertEqual(sent["source_message"]["content"], "CSV")
        self.assertEqual(sent["recent_messages"], [])
        third_plan = self._answer(second.configuration_item_id, "不需要提醒", "answer-2")
        self.chat.return_value = json.dumps(valid_spec())
        third = self._execute_plan(third_plan)
        self.assertEqual(self._status().state, "awaiting_approval")
        self.assertEqual(self._status().result, third)
        self.assertEqual(self.chat.call_count, 3)
        with self.session_factory() as db:
            items = db.scalars(select(ConfigurationItem).order_by(ConfigurationItem.version)).all()
            self.assertEqual([item.version for item in items], [1, 2, 3])
            self.assertEqual(
                [item.upstream_item_ids for item in items],
                [[], [first.configuration_item_id], [second.configuration_item_id]],
            )
            self.assertTrue(all(item.state == "usable" for item in items))
            self.assertEqual(items[0].payload["open_questions"], ["导出格式？", "是否需要提醒？"])
            plans = db.scalars(select(Plan).order_by(Plan.version)).all()
            self.assertEqual([plan.version for plan in plans], [1, 2, 3])
            for plan in plans:
                result = db.scalar(
                    select(TaskResult).join(Task).where(Task.plan_id == plan.plan_id)
                )
                self.assertEqual(result.source_message_ids, [plan.cause_message_id])
            run = db.get(BuildRun, self.run.id)
            self.assertEqual((run.status, run.stage, run.active_slot), ("running", "pm", 1))
        with self.assertRaises(ConflictException):
            self._run()  # 旧执行结果不能作为当前进度重放。

    def test_same_answer_replays_one_plan_and_different_answer_is_rejected(self):
        result = self._questions()
        first = self._answer(result.configuration_item_id)
        counts = self._counts()
        self.assertEqual(self._answer(result.configuration_item_id).plan_id, first.plan_id)
        for content, key in [("JSON", "answer-1"), ("CSV", "answer-other")]:
            with self.assertRaises(ConflictException):
                self._answer(result.configuration_item_id, content, key)
        self.assertEqual(self._counts(), counts)

    def test_answer_rejects_outsider_missing_and_approved_sources(self):
        result = self._run()
        counts = self._counts()
        with self.assertRaises(NotFoundException):
            self._answer(result.configuration_item_id, user=self.outsider)
        with self.assertRaises(NotFoundException):
            self._answer("ci_missing")
        with self.session_factory() as db:
            approve_requirements(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                result.configuration_item_id,
                RequirementsApproval.model_validate(
                    approval_payload(client_message_id="approve-then-block-answer")
                ),
            )
        with self.assertRaises(ConflictException):
            self._answer(result.configuration_item_id)
        self.assertEqual(self._counts()[:3], (counts[0] + 1, counts[1] + 1, counts[2] + 1))

    def test_answer_transaction_rolls_back_message_plan_and_tasks_together(self):
        result = self._questions()
        counts = self._counts()

        def fail(*_):
            raise RuntimeError("fail clarification link")

        event.listen(RequirementClarification, "before_update", fail)
        try:
            with self.assertRaisesRegex(RuntimeError, "fail clarification link"):
                self._answer(result.configuration_item_id)
        finally:
            event.remove(RequirementClarification, "before_update", fail)
        self.assertEqual(self._counts(), counts)
        self.assertEqual(self._status().state, "awaiting_approval")
        self._answer(result.configuration_item_id)

    def test_legacy_question_result_can_receive_an_answer(self):
        result = self._questions()
        with self.session_factory() as db:
            db.delete(db.get(RequirementClarification, result.configuration_item_id))
            db.commit()
        plan = self._answer(result.configuration_item_id)
        with self.session_factory() as db:
            link = db.get(RequirementClarification, result.configuration_item_id)
            self.assertEqual(link.followup_plan_id, plan.plan_id)

    def test_concurrent_same_answer_creates_only_one_followup(self):
        result = self._questions()
        barrier = Barrier(2)

        def submit():
            barrier.wait(timeout=10)
            return self._answer(result.configuration_item_id).plan_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            ids = [future.result(timeout=20) for future in futures]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self._counts(), (3, 2, 2, 1, 1, 1))

    def test_concurrent_different_answers_have_one_winner(self):
        result = self._questions()
        barrier = Barrier(2)

        def submit(index):
            barrier.wait(timeout=10)
            try:
                return self._answer(
                    result.configuration_item_id, f"格式{index}", f"key-{index}"
                ).plan_id
            except ConflictException:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, index) for index in range(2)]
            ids = [future.result(timeout=20) for future in futures]
        self.assertEqual(sum(value is not None for value in ids), 1)
        self.assertEqual(self._counts(), (3, 2, 2, 1, 1, 1))

    def test_duplicate_execution_while_model_running_does_not_call_model_twice(self):
        entered, release = Event(), Event()

        def generate(**_):
            entered.set()
            self.assertTrue(release.wait(timeout=20))
            return json.dumps(valid_spec())

        self.chat.side_effect = generate
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._run)
            try:
                self.assertTrue(entered.wait(timeout=10))
                self.assertEqual(self._status().state, "running")
                with self.assertRaisesRegex(ConflictException, "正在处理"):
                    self._run()
                self.assertEqual(self.chat.call_count, 1)
            finally:
                release.set()
            future.result(timeout=20)
        self.assertEqual(self._status().state, "awaiting_approval")

    def test_expired_execution_is_fenced_and_needs_explicit_recovery(self):
        _, pending = self._create_plan()
        with self.session_factory() as db:
            task.claim_product_manager_task(
                db, self.owner, self.project.id, self.run.run_id, pending.task_id
            )
            old = task_execution.start_execution(
                db, self.owner, self.project.id, self.run.run_id, pending.task_id
            )
            draft = product_manager.generate_task_app_spec(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                pending.task_id,
                execution_id=old.execution_id,
            )
            old.expires_at = task_execution.utc_now() - timedelta(seconds=1)
            db.commit()
        self.assertEqual(self._status().state, "retry_available")
        with self.assertRaises(ConflictException):
            self._run()
        with self.session_factory() as db:
            current = task_execution.start_execution(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                pending.task_id,
                recovery_execution_id=old.execution_id,
            )
            with self.assertRaises(ConflictException):
                product_manager.complete_task_app_spec(
                    db, self.owner, self.project.id, self.run.run_id, pending.task_id, draft
                )
            task_execution.fail_execution(db, pending.task_id, old.execution_id)
            db.refresh(current)
            self.assertEqual(current.status, "running")
            with self.assertRaises(ConflictException):
                product_manager.generate_task_app_spec(
                    db, self.owner, self.project.id, self.run.run_id, pending.task_id
                )
            fresh = product_manager.generate_task_app_spec(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                pending.task_id,
                execution_id=current.execution_id,
            )
            product_manager.complete_task_app_spec(
                db, self.owner, self.project.id, self.run.run_id, pending.task_id, fresh
            )
            self.assertEqual(db.get(TaskExecution, old.execution_id).status, "superseded")
        self.assertEqual(self._status().state, "awaiting_approval")

    def test_recovery_reuses_persisted_draft_after_publication_failure(self):
        with patch.object(
            product_manager, "complete_task_app_spec", side_effect=RuntimeError("save failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "save failed"):
                self._run()
        progress = self._status()
        self.assertEqual(progress.state, "retry_available")
        with self.session_factory() as db:
            self.assertIsNotNone(db.get(TaskExecution, progress.execution_id).draft)
        result = self._run(recovery_execution_id=progress.execution_id)
        self.chat.assert_called_once()
        with self.session_factory() as db:
            attempts = db.scalars(select(TaskExecution).order_by(TaskExecution.attempt)).all()
            self.assertEqual([entry.status for entry in attempts], ["failed", "succeeded"])
            self.assertEqual(db.scalar(select(func.count()).select_from(ConfigurationItem)), 1)
            draft = ProductManagerResult.model_validate(attempts[-1].draft)
            replay = product_manager.complete_task_app_spec(
                db, self.owner, self.project.id, self.run.run_id, result.task_id, draft
            )
            self.assertEqual(replay.item_id, result.configuration_item_id)

    def test_workflow_does_not_discard_callers_unsaved_edits(self):
        with self.session_factory() as db:
            project = db.get(Project, self.project.id)
            project.name = "待保存的编辑"
            with self.assertRaises(BusinessException):
                run_product_manager_workflow(
                    db, self.owner, self.project.id, self.run.run_id, self.message.id
                )
            self.assertEqual(project.name, "待保存的编辑")
            self.assertIn(project, db.dirty)
        self.chat.assert_not_called()

    def test_migration_matches_models_preserves_old_rows_and_supports_downgrade(self):
        self._create_plan()
        path = (
            Path(__file__).resolve().parents[1]
            / "alembic/versions/5c9d3e7f1a2b_add_requirements_execution.py"
        )
        spec = importlib.util.spec_from_file_location("requirements_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.engine.begin() as connection:
            RequirementClarification.__table__.drop(connection)
            TaskExecution.__table__.drop(connection)
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            self.assertEqual(
                compare_metadata(MigrationContext.configure(connection), Base.metadata), []
            )
            self.assertEqual(connection.scalar(select(func.count()).select_from(Task)), 1)
            migration.downgrade()
            migration.upgrade()
            self.assertEqual(
                compare_metadata(MigrationContext.configure(connection), Base.metadata), []
            )
            self.assertEqual(connection.scalar(select(func.count()).select_from(ProjectMessage)), 2)
        result = self._questions()
        self._answer(result.configuration_item_id)
        with self.session_factory() as db:
            db.delete(db.get(Project, self.project.id))
            db.commit()
            self.assertEqual(db.scalar(select(func.count()).select_from(TaskExecution)), 0)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(RequirementClarification)), 0
            )

    def test_execution_unique_slot_rejects_second_running_attempt(self):
        _, pending = self._create_plan()
        with self.session_factory() as db:
            task.claim_product_manager_task(
                db, self.owner, self.project.id, self.run.run_id, pending.task_id
            )
            first = task_execution.start_execution(
                db, self.owner, self.project.id, self.run.run_id, pending.task_id
            )
            db.add(
                TaskExecution(
                    execution_id="exec_duplicate",
                    task_id=pending.task_id,
                    attempt=2,
                    status="running",
                    active_slot=1,
                    started_at=first.started_at,
                    expires_at=first.expires_at,
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()


class RequirementsApiTests(ProductManagerWorkflowFixture):
    def setUp(self):
        super().setUp()
        self.current_user = self.owner
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(api_router)

        def database():
            with self.session_factory() as db:
                yield db

        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user] = lambda: self.current_user
        self.client = self.enterContext(TestClient(app))
        self.base = f"/api/v1/projects/{self.project.id}"
        self.execute = f"{self.base}/build-runs/{self.run.run_id}/requirements"

    def test_http_questions_answer_replay_and_page_refresh(self):
        before = self.client.get(f"{self.base}/requirements")
        self.assertEqual(before.json()["data"]["state"], "not_started")
        self.chat.assert_not_called()
        self.chat.return_value = json.dumps(valid_spec(open_questions=["导出格式？"]))
        first = self.client.post(self.execute, json={"message_id": self.message.id})
        self.assertEqual(first.status_code, 200, first.text)
        item_id = first.json()["data"]["configuration_item_id"]
        waiting = self.client.get(f"{self.base}/requirements")
        self.assertEqual(waiting.json()["data"]["result"]["open_questions"], ["导出格式？"])
        self.chat.return_value = json.dumps(valid_spec())
        answer = {"content": "CSV", "client_message_id": "api-answer"}
        response = self.client.post(f"{self.execute}/{item_id}/answers", json=answer)
        self.assertEqual(response.status_code, 200, response.text)
        replay = self.client.post(f"{self.execute}/{item_id}/answers", json=answer)
        self.assertEqual(response.json(), replay.json())
        current = self.client.get(f"{self.base}/requirements").json()["data"]
        self.assertEqual(current["state"], "awaiting_approval")
        self.assertEqual(
            current["result"]["configuration_item_id"],
            response.json()["data"]["configuration_item_id"],
        )
        self.assertEqual(current["app_spec"], valid_spec())
        approval = self.client.post(
            f"{self.execute}/{current['result']['configuration_item_id']}/approval",
            json=approval_payload(client_message_id="api-approval"),
        )
        self.assertEqual(approval.status_code, 200, approval.text)
        self.assertEqual(approval.json()["data"]["state"], "design_pending")
        replay_approval = self.client.post(
            f"{self.execute}/{current['result']['configuration_item_id']}/approval",
            json=approval_payload(client_message_id="api-approval"),
        )
        self.assertEqual(replay_approval.status_code, 200, replay_approval.text)
        self.assertEqual(replay_approval.json(), approval.json())
        self.assertEqual(self.chat.call_count, 2)

    def test_http_retries_dispatch_from_approved_requirements_without_another_model_call(self):
        first = self.client.post(self.execute, json={"message_id": self.message.id})
        self.assertEqual(first.status_code, 200, first.text)
        item_id = first.json()["data"]["configuration_item_id"]
        with (
            self.assertLogs("forgeai", level="ERROR"),
            patch(
                "app.api.v1.requirements.create_engineering_delivery_task",
                side_effect=RuntimeError("dispatch failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "dispatch failed"):
                self.client.post(
                    f"{self.execute}/{item_id}/approval",
                    json=approval_payload(client_message_id="dispatch-approval"),
                )
        waiting = self.client.get(f"{self.base}/requirements")
        self.assertEqual(waiting.status_code, 200)
        saved = waiting.json()["data"]
        self.assertEqual(saved["state"], "ready_for_design")
        retry = self.client.post(self.execute, json={"message_id": saved["message_id"]})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(
            retry.json()["data"]["configuration_item_id"], saved["result"]["configuration_item_id"]
        )
        self.assertIsNotNone(retry.json()["data"]["design_task_id"])
        self.chat.assert_called_once()

    def test_http_rejects_cross_owner_and_malformed_input(self):
        self.current_user = self.outsider
        for method, path, payload in [
            ("GET", f"{self.base}/requirements", None),
            ("POST", self.execute, {"message_id": self.message.id}),
            (
                "POST",
                f"{self.execute}/ci_missing/answers",
                {"content": "CSV", "client_message_id": "x"},
            ),
        ]:
            response = self.client.request(method, path, json=payload)
            self.assertEqual(response.status_code, 404, response.text)
        self.current_user = self.owner
        for payload in [
            {"message_id": 0},
            {"message_id": self.message.id, "user_id": self.outsider.id},
        ]:
            self.assertEqual(self.client.post(self.execute, json=payload).status_code, 422)
        self.chat.assert_not_called()
