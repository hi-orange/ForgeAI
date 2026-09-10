import importlib.util
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, func, insert, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.agents import product_manager as product_manager_agent
from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.db.database import Base
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.project import Project
from app.models.project_message import ProjectMessage
from app.models.project_message_classification import ProjectMessageClassification
from app.models.task import Task
from app.models.task_result import TaskResult
from app.models.user import User
from app.schemas.configuration_item import ConfigurationItemRegistration
from app.schemas.plan import PlanCreate
from app.schemas.product_manager import ProductManagerResult
from app.schemas.project import ProjectCreate
from app.schemas.task import TaskCreate
from app.services import build_run as build_run_service
from app.services import configuration_manager
from app.services import plan as plan_service
from app.services import product_manager as product_manager_service
from app.services import project as project_service
from app.services import project_manager as project_manager_service
from app.services import task as task_service


class TaskCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory(prefix="forgeai-task-completion-test-"))
        self.engine = create_engine(
            f"sqlite+pysqlite:///{Path(directory) / 'completion.db'}",
            connect_args={"check_same_thread": False, "timeout": 10},
        )
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        spec = {
            "goal": "记录阅读进度",
            "target_users": ["个人读者"],
            "features": ["记录书名和阅读进度"],
            "data_requirements": ["书名、阅读进度"],
            "interface_requirements": [],
            "constraints": [],
            "acceptance_criteria": ["保存后能查看书名和阅读进度"],
            "open_questions": [],
        }
        self.chat = self.enterContext(
            patch.object(product_manager_agent, "chat_completion", return_value=json.dumps(spec))
        )
        with self.session_factory() as db:
            self.owner = User(username="owner", email="owner@test.com", hashed_password="unused")
            self.outsider = User(
                username="outsider", email="outsider@test.com", hashed_password="unused"
            )
            db.add_all([self.owner, self.outsider])
            db.commit()
            self.project, self.run, self.message, self.plan, self.task = self._seed_project(
                db, "阅读记录"
            )
            self.other_project, self.other_run, self.other_message, _, self.other_task = (
                self._seed_project(db, "另一个项目")
            )
            self.draft = product_manager_service.generate_task_app_spec(
                db, self.owner, self.project.id, self.run.run_id, self.task.task_id
            )
        self.chat.reset_mock()

    def _seed_project(self, db, name):
        project = project_service.create_project(db, self.owner, ProjectCreate(prompt=name))
        run = build_run_service.create_build_run(db, self.owner, project.id)
        message = db.scalar(select(ProjectMessage).where(ProjectMessage.project_id == project.id))
        db.add(
            ProjectMessageClassification(
                message_id=message.id,
                category="product_change",
                decision_summary="首次需求",
                classifier_model="test-model",
                prompt_version="test-v1",
            )
        )
        db.commit()
        plan = project_manager_service.create_initial_plan(
            db, self.owner, project.id, run.run_id, message.id
        )
        task = task_service.list_user_plan_tasks(db, self.owner, project.id, plan.plan_id)[0]
        task_service.claim_product_manager_task(
            db, self.owner, project.id, run.run_id, task.task_id
        )
        db.refresh(run)
        return project, run, message, plan, task

    def _complete(self, db, result=None, **overrides):
        return product_manager_service.complete_task_app_spec(
            db,
            overrides.get("user", self.owner),
            overrides.get("project_id", self.project.id),
            overrides.get("run_id", self.run.run_id),
            overrides.get("task_id", self.task.task_id),
            result if result is not None else self.draft,
        )

    def _counts(self, *, items=0, results=0):
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ConfigurationItem)), items)
            self.assertEqual(db.scalar(select(func.count()).select_from(TaskResult)), results)

    def _assert_unfinished(self):
        self._counts()
        with self.session_factory() as db:
            self.assertEqual(db.get(Task, self.task.id).status, "running")
            self.assertEqual(db.get(Plan, self.plan.id).status, "running")
            self.assertEqual(db.get(BuildRun, self.run.id).status, "running")

    def _two_task_plan(self, db):
        db.get(Plan, self.plan.id).status = "cancelled"
        db.get(Task, self.task.id).status = "cancelled"
        db.commit()
        plan = plan_service.save_plan(
            db,
            self.owner,
            self.project.id,
            self.run.run_id,
            PlanCreate(
                version=2,
                cause_message_id=self.message.id,
                tasks=[
                    TaskCreate(
                        task_key=key,
                        recipient="ProductManager",
                        title=key,
                        instructions="整理指定需求",
                        expected_output_type="app_spec",
                    )
                    for key in ("one", "two")
                ],
            ),
        )
        tasks = task_service.list_user_plan_tasks(db, self.owner, self.project.id, plan.plan_id)
        drafts = []
        for task in tasks:
            task_service.claim_product_manager_task(
                db, self.owner, self.project.id, self.run.run_id, task.task_id
            )
            drafts.append(
                product_manager_service.generate_task_app_spec(
                    db, self.owner, self.project.id, self.run.run_id, task.task_id
                )
            )
        return plan, tasks, drafts

    def test_saves_body_provenance_and_completion_in_one_commit_without_finishing_run(self):
        with self.session_factory() as db:
            run_before = db.get(BuildRun, self.run.id).updated_at
            with patch.object(db, "commit", wraps=db.commit) as commit:
                item = self._complete(db)
                commit.assert_called_once_with()
            self.assertEqual(item.payload, self.draft.app_spec.model_dump(mode="json"))
            self.assertEqual(
                (item.semantic_type, item.version, item.schema_version), ("app_spec", 1, 2)
            )
            self.assertEqual(
                (item.project_id, item.producer_run_id), (self.project.id, self.run.run_id)
            )
            self.assertEqual(item.upstream_item_ids, [])
            self.assertEqual(item.state, "usable")
            record = task_service.get_user_task_result(
                db, self.owner, self.project.id, self.task.task_id
            )
            self.assertEqual(record.configuration_item_id, item.item_id)
            self.assertEqual(record.source_message_ids, self.draft.source_message_ids)
            self.assertEqual(record.model, self.draft.model)
            self.assertEqual(record.prompt_version, self.draft.prompt_version)
            self.assertEqual(record.context_truncated, self.draft.context_truncated)
            self.assertEqual(len(record.result_hash), 64)
            self.assertEqual(db.get(Task, self.task.id).status, "succeeded")
            self.assertEqual(db.get(Plan, self.plan.id).status, "succeeded")
            run = db.get(BuildRun, self.run.id)
            self.assertEqual(
                (run.status, run.stage, run.active_slot, run.updated_at),
                ("running", "pm", 1, run_before),
            )
            self.assertEqual(db.get(Project, self.project.id).status, "draft")
            self.assertEqual(db.get(Task, self.other_task.id).status, "running")
        self._counts(items=1, results=1)
        self.chat.assert_not_called()

    def test_same_result_replays_without_new_versions_or_timestamp_changes(self):
        with self.session_factory() as db:
            item = self._complete(db)
            timestamps = (
                db.get(Task, self.task.id).updated_at,
                db.get(Plan, self.plan.id).updated_at,
            )
        with self.session_factory() as db:
            replay = self._complete(
                db, ProductManagerResult.model_validate_json(self.draft.model_dump_json())
            )
            self.assertEqual(replay.item_id, item.item_id)
            self.assertEqual(
                (db.get(Task, self.task.id).updated_at, db.get(Plan, self.plan.id).updated_at),
                timestamps,
            )
        self._counts(items=1, results=1)

    def test_different_body_or_metadata_cannot_replace_registered_result(self):
        with self.session_factory() as db:
            original = self._complete(db)
        for changed in (
            {"app_spec": {**self.draft.app_spec.model_dump(), "goal": "不同的目标"}},
            {"model": "different-model"},
            {"prompt_version": "different-prompt"},
        ):
            with self.subTest(changed=changed), self.session_factory() as db:
                result = ProductManagerResult.model_validate({**self.draft.model_dump(), **changed})
                with self.assertRaisesRegex(ConflictException, "不能覆盖"):
                    self._complete(db, result)
                self.assertEqual(
                    db.get(ConfigurationItem, original.id).payload, self.draft.app_spec.model_dump()
                )
        self._counts(items=1, results=1)

    def test_replay_of_terminal_run_and_unusable_item_does_not_revive_them(self):
        with self.session_factory() as db:
            original = self._complete(db)
            run = db.get(BuildRun, self.run.id)
            run.status, run.active_slot = "failed", None
            db.commit()
            configuration_manager.mark_run_configuration_items_unusable(
                db, project_id=self.project.id, producer_run_id=self.run.run_id, reason="需求变更"
            )
            newer = build_run_service.create_build_run(db, self.owner, self.project.id)
            replay = self._complete(db)
            self.assertEqual((replay.item_id, replay.state), (original.item_id, "unusable"))
            self.assertEqual(db.get(BuildRun, self.run.id).status, "failed")
            self.assertEqual(db.get(BuildRun, newer.id).status, "queued")
        self._counts(items=1, results=1)

    def test_all_tasks_must_succeed_before_plan_succeeds(self):
        with self.session_factory() as db:
            plan, tasks, drafts = self._two_task_plan(db)
            first = self._complete(db, drafts[0], task_id=tasks[0].task_id)
            self.assertEqual(db.get(Plan, plan.id).status, "running")
            self.assertEqual(db.get(Task, tasks[1].id).status, "running")
            second = self._complete(db, drafts[1], task_id=tasks[1].task_id)
            self.assertEqual((first.version, second.version), (1, 2))
            self.assertEqual(db.get(Plan, plan.id).status, "succeeded")
            self.assertEqual(db.get(BuildRun, self.run.id).status, "running")
        self._counts(items=2, results=2)

    def test_cancelled_sibling_does_not_count_as_success(self):
        with self.session_factory() as db:
            plan, tasks, drafts = self._two_task_plan(db)
            db.get(Task, tasks[1].id).status = "cancelled"
            db.commit()
            self._complete(db, drafts[0], task_id=tasks[0].task_id)
            self.assertEqual(db.get(Plan, plan.id).status, "running")

    def test_owner_and_explicit_result_ids_are_checked(self):
        for overrides in ({"user": self.outsider}, {"project_id": 999999}):
            with self.subTest(overrides=overrides), self.session_factory() as db:
                with self.assertRaises(NotFoundException):
                    self._complete(db, **overrides)
        for changed in (
            {"project_id": self.other_project.id},
            {"build_run_id": self.other_run.run_id},
            {"task_id": self.other_task.task_id},
            {"plan_id": "plan_wrong"},
            {
                "cause_message_id": self.other_message.id,
                "source_message_ids": [self.other_message.id],
            },
        ):
            with self.subTest(changed=changed), self.session_factory() as db:
                result = ProductManagerResult.model_validate({**self.draft.model_dump(), **changed})
                with self.assertRaises(BusinessException):
                    self._complete(db, result)
        self._assert_unfinished()

    def test_missing_and_foreign_tasks_cannot_receive_another_result(self):
        for task_id in ("task_missing", self.other_task.task_id):
            with self.subTest(task_id=task_id), self.session_factory() as db:
                result = self.draft.model_copy(update={"task_id": task_id})
                with self.assertRaises(NotFoundException):
                    self._complete(db, result, task_id=task_id)
        self._assert_unfinished()

    def test_source_window_is_checked_and_newer_messages_are_not_used(self):
        with self.session_factory() as db:
            future = ProjectMessage(
                project_id=self.project.id,
                sequence=2,
                sender="user",
                content="后来的新需求",
                client_message_id="future",
            )
            db.add(future)
            db.commit()
            for changes in (
                {"source_message_ids": [future.id, self.message.id]},
                {"source_message_ids": [self.other_message.id, self.message.id]},
                {"context_truncated": True},
            ):
                with self.subTest(changes=changes):
                    result = ProductManagerResult.model_validate(
                        {**self.draft.model_dump(), **changes}
                    )
                    with self.assertRaisesRegex(BusinessException, "消息来源"):
                        self._complete(db, result)
            self._complete(db)
            self.assertEqual(
                db.get(TaskResult, self.task.task_id).source_message_ids, [self.message.id]
            )

    def test_mutated_invalid_result_is_revalidated_before_writes(self):
        for mutate in (
            lambda result: setattr(result.app_spec, "goal", " "),
            lambda result: result.source_message_ids.append(result.cause_message_id),
            lambda result: setattr(result, "model", " "),
        ):
            with self.subTest(mutate=mutate), self.session_factory() as db:
                result = self.draft.model_copy(deep=True)
                mutate(result)
                with self.assertRaisesRegex(BusinessException, "提交结果不符合要求"):
                    self._complete(db, result)
        self._assert_unfinished()

    def test_stale_session_cannot_submit_after_task_plan_or_run_loses_eligibility(self):
        cases = [
            (Task, self.task.id, {"status": state})
            for state in ("pending", "cancelled", "failed", "succeeded")
        ] + [
            (Plan, self.plan.id, {"status": "cancelled"}),
            (BuildRun, self.run.id, {"status": "failed", "active_slot": None}),
            (BuildRun, self.run.id, {"stage": "architect"}),
        ]
        for model, row_id, changes in cases:
            with self.subTest(model=model.__name__, changes=changes), self.session_factory() as db:
                cached = db.get(model, row_id)
                original = {field: getattr(cached, field) for field in changes}
                with self.session_factory() as writer:
                    row = writer.get(model, row_id)
                    for field, value in changes.items():
                        setattr(row, field, value)
                    writer.commit()
                with self.assertRaises(ConflictException):
                    self._complete(db)
                with self.session_factory() as writer:
                    row = writer.get(model, row_id)
                    for field, value in original.items():
                        setattr(row, field, value)
                    writer.commit()
                self._assert_unfinished()

    def test_each_write_failure_rolls_back_body_association_and_statuses(self):
        def fail_write(_mapper, _connection, _target):
            raise RuntimeError("forced write failure")

        for model, event_name in (
            (ConfigurationItem, "before_insert"),
            (TaskResult, "before_insert"),
            (Plan, "before_update"),
        ):
            with self.subTest(model=model.__name__):
                event.listen(model, event_name, fail_write)
                try:
                    with self.session_factory() as db:
                        with self.assertRaisesRegex(RuntimeError, "forced write failure"):
                            self._complete(db)
                        self.assertFalse(db.in_transaction())
                finally:
                    event.remove(model, event_name, fail_write)
                self._assert_unfinished()
        with self.session_factory() as db:
            self._complete(db)
        self._counts(items=1, results=1)

    def test_commit_failure_rolls_back_all_changes(self):
        with self.session_factory() as db:
            with patch.object(db, "commit", side_effect=RuntimeError("commit failed")):
                with self.assertRaisesRegex(RuntimeError, "commit failed"):
                    self._complete(db)
        self._assert_unfinished()

    def test_completion_flushes_statuses_with_production_autoflush_disabled(self):
        factory = sessionmaker(bind=self.engine, autoflush=False)
        with factory() as db:
            item = self._complete(db)
            self.assertEqual(db.get(Task, self.task.id).status, "succeeded")
            self.assertEqual(db.get(Plan, self.plan.id).status, "succeeded")
            self.assertEqual(
                db.get(TaskResult, self.task.task_id).configuration_item_id, item.item_id
            )
        self._counts(items=1, results=1)

    def test_database_insert_conflict_rolls_back_without_leaving_result_or_success_state(self):
        with self.session_factory() as db:
            existing = configuration_manager.register_configuration_item(
                db,
                project_id=self.project.id,
                producer_run_id=self.run.run_id,
                submission=ConfigurationItemRegistration(
                    semantic_type="app_spec", payload={"old": True}
                ),
            )
            with patch.object(configuration_manager, "uuid4") as uuid:
                uuid.return_value.hex = existing.item_id.removeprefix("ci_")
                with self.assertRaisesRegex(ConflictException, "成果保存冲突"):
                    self._complete(db)
            self.assertEqual(db.get(Task, self.task.id).status, "running")
            self.assertEqual(db.get(Plan, self.plan.id).status, "running")
        self._counts(items=1, results=0)

    def test_staged_registration_does_not_commit_and_uses_fresh_run_state(self):
        submission = ConfigurationItemRegistration(
            semantic_type="app_spec", payload={"goal": "staged"}
        )
        with self.session_factory() as db:
            item = configuration_manager.stage_configuration_item(
                db,
                project_id=self.project.id,
                producer_run_id=self.run.run_id,
                submission=submission,
            )
            self.assertEqual(item.state, "usable")
            self._counts()
            db.rollback()
        self._assert_unfinished()
        with self.session_factory() as db:
            cached = db.get(BuildRun, self.run.id)
            with self.session_factory() as writer:
                run = writer.get(BuildRun, self.run.id)
                run.status, run.active_slot = "failed", None
                writer.commit()
            self.assertEqual(cached.status, "running")
            item = configuration_manager.register_configuration_item(
                db,
                project_id=self.project.id,
                producer_run_id=self.run.run_id,
                submission=submission,
            )
            self.assertEqual(item.state, "unusable")

    def test_result_lookup_is_owner_scoped_and_missing_result_is_not_found(self):
        with self.session_factory() as db:
            with self.assertRaises(NotFoundException):
                task_service.get_user_task_result(
                    db, self.owner, self.project.id, self.task.task_id
                )
            item = self._complete(db)
            self.assertEqual(
                task_service.get_user_task_result(
                    db, self.owner, self.project.id, self.task.task_id
                ).configuration_item_id,
                item.item_id,
            )
            for user, project_id in (
                (self.outsider, self.project.id),
                (self.owner, self.other_project.id),
            ):
                with self.assertRaises(NotFoundException):
                    task_service.get_user_task_result(db, user, project_id, self.task.task_id)

    def test_result_record_is_immutable_including_in_place_source_edits(self):
        with self.session_factory() as db:
            self._complete(db)
        for field, value in (
            ("model", "changed"),
            ("result_hash", "a" * 64),
            ("configuration_item_id", "ci_wrong"),
            ("source_message_ids", []),
        ):
            with self.subTest(field=field), self.session_factory() as db:
                setattr(db.get(TaskResult, self.task.task_id), field, value)
                with self.assertRaisesRegex(ValueError, "不可修改"):
                    db.commit()
                db.rollback()
        with self.session_factory() as db:
            record = db.get(TaskResult, self.task.task_id)
            record.source_message_ids.append(999)
            with self.assertRaises(ValueError):
                db.commit()
            db.rollback()

    def test_database_enforces_unique_task_unique_item_and_foreign_keys(self):
        with self.session_factory() as db:
            item = self._complete(db)
        base = dict(
            task_id=self.other_task.task_id,
            configuration_item_id=item.item_id,
            result_hash="a" * 64,
            source_message_ids=[self.message.id],
            context_truncated=False,
            model="test",
            prompt_version="v1",
        )
        for changed in (
            {},
            {"task_id": self.task.task_id},
            {"task_id": "task_missing"},
            {"configuration_item_id": "ci_missing"},
        ):
            with self.subTest(changed=changed), self.session_factory() as db:
                with self.assertRaises(IntegrityError):
                    db.execute(insert(TaskResult).values(**{**base, **changed}))
                    db.commit()
                db.rollback()

    def test_project_deletion_cascades_without_touching_other_project(self):
        with self.session_factory() as db:
            item = self._complete(db)
            db.delete(db.get(Project, self.project.id))
            db.commit()
        with self.session_factory() as db:
            self.assertIsNone(db.get(TaskResult, self.task.task_id))
            self.assertIsNone(db.get(ConfigurationItem, item.id))
            self.assertIsNotNone(db.get(Project, self.other_project.id))

    def test_migration_matches_model_and_preserves_prior_rows(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "4e8a2c6d0f1b_create_task_result.py"
        )
        spec = importlib.util.spec_from_file_location("task_result_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        self.assertEqual(migration.down_revision, "9b3e5f7a1c2d")
        with self.engine.begin() as connection:
            TaskResult.__table__.drop(connection)
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            self.assertEqual(
                compare_metadata(MigrationContext.configure(connection), Base.metadata), []
            )
        with self.session_factory() as db:
            self._complete(db)
        with self.engine.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.downgrade()
            self.assertNotIn("task_result", inspect(connection).get_table_names())
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(ConfigurationItem)), 1
            )
            self.assertEqual(connection.scalar(select(func.count()).select_from(ProjectMessage)), 2)
            migration.upgrade()
            self.assertEqual(
                compare_metadata(MigrationContext.configure(connection), Base.metadata), []
            )

    def _concurrent_complete(self, first, second):
        barrier = Barrier(2)

        def wait_before_lock(_connection, _cursor, statement, _params, _context, _many):
            if statement.startswith("UPDATE build_run "):
                barrier.wait(timeout=10)

        def complete_once(draft):
            with self.session_factory() as db:
                try:
                    return self._complete(db, draft, task_id=draft.task_id).item_id
                except ConflictException:
                    return None

        event.listen(self.engine, "before_cursor_execute", wait_before_lock)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                left, right = (
                    executor.submit(complete_once, first),
                    executor.submit(complete_once, second),
                )
                return left.result(timeout=20), right.result(timeout=20)
        finally:
            event.remove(self.engine, "before_cursor_execute", wait_before_lock)

    def test_same_result_concurrently_creates_one_body_and_one_record(self):
        left, right = self._concurrent_complete(self.draft, self.draft.model_copy(deep=True))
        self.assertIsNotNone(left)
        self.assertEqual(left, right)
        self._counts(items=1, results=1)

    def test_different_results_concurrently_have_one_winner_without_orphan_body(self):
        changed = self.draft.model_copy(deep=True)
        changed.app_spec.goal = "不同目标"
        outcomes = self._concurrent_complete(self.draft, changed)
        self.assertEqual(outcomes.count(None), 1)
        self._counts(items=1, results=1)

    def test_last_two_tasks_can_complete_concurrently_and_finish_plan(self):
        with self.session_factory() as db:
            plan, _, drafts = self._two_task_plan(db)
        left, right = self._concurrent_complete(*drafts)
        self.assertIsNotNone(left)
        self.assertIsNotNone(right)
        self.assertNotEqual(left, right)
        self._counts(items=2, results=2)
        with self.session_factory() as db:
            self.assertEqual(db.get(Plan, plan.id).status, "succeeded")
            self.assertEqual(db.get(BuildRun, self.run.id).status, "running")


if __name__ == "__main__":
    unittest.main()
