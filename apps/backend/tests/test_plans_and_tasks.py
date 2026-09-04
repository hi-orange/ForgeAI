import importlib.util
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, inspect, null, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.db.database import Base
from app.models.build_run import BuildRun, BuildRunStage, BuildRunStatus
from app.models.configuration_item import ConfigurationItem, ConfigurationItemType
from app.models.plan import Plan, PlanStatus
from app.models.project import Project
from app.models.project_message import ProjectMessage
from app.models.task import Task, TaskRecipient, TaskStatus
from app.models.user import User
from app.schemas.plan import PlanCreate, PlanOut
from app.schemas.task import TaskCreate, TaskOut
from app.services import plan as plan_service
from app.services import task as task_service


def enable_foreign_keys(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class PlanTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(self.engine, "connect", enable_foreign_keys)
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.session_factory() as db:
            self.owner = User(username="owner", email="owner@test.com", hashed_password="unused")
            self.outsider = User(
                username="outsider", email="outsider@test.com", hashed_password="unused"
            )
            db.add_all([self.owner, self.outsider])
            db.flush()
            self.project = Project(user_id=self.owner.id, name="Demo", status="available")
            self.other_project = Project(user_id=self.owner.id, name="Other", status="draft")
            db.add_all([self.project, self.other_project])
            db.flush()
            self.run = BuildRun(project_id=self.project.id, run_id="run_current")
            self.other_run = BuildRun(project_id=self.other_project.id, run_id="run_other")
            self.history_run = BuildRun(
                project_id=self.project.id,
                run_id="run_history",
                status=BuildRunStatus.SUCCEEDED.value,
                stage=BuildRunStage.PM.value,
                active_slot=null(),
            )
            self.message = ProjectMessage(
                project_id=self.project.id,
                sequence=1,
                sender="user",
                content="Add a catalog",
                client_message_id="initial",
            )
            self.assistant_message = ProjectMessage(
                project_id=self.project.id,
                sequence=2,
                sender="assistant",
                content="Working",
                client_message_id="reply",
            )
            self.other_message = ProjectMessage(
                project_id=self.other_project.id,
                sequence=1,
                sender="user",
                content="Other requirement",
                client_message_id="other",
            )
            db.add_all(
                [
                    self.run,
                    self.other_run,
                    self.history_run,
                    self.message,
                    self.assistant_message,
                    self.other_message,
                ]
            )
            db.flush()
            self.input_item = ConfigurationItem(
                item_id="ci_base",
                project_id=self.project.id,
                producer_run_id=self.history_run.run_id,
                semantic_type="app_spec",
                version=1,
                payload={"name": "base"},
                content_hash="a" * 64,
                upstream_item_ids=[],
            )
            self.unusable_item = ConfigurationItem(
                item_id="ci_unusable",
                project_id=self.project.id,
                producer_run_id=self.history_run.run_id,
                semantic_type="app_spec",
                version=2,
                payload={"name": "unusable"},
                content_hash="b" * 64,
                upstream_item_ids=[],
                state="unusable",
                unusable_reason="superseded input",
                unusable_at=datetime(2026, 9, 4),
            )
            self.other_item = ConfigurationItem(
                item_id="ci_other",
                project_id=self.other_project.id,
                producer_run_id=self.other_run.run_id,
                semantic_type="app_spec",
                version=1,
                payload={"name": "other"},
                content_hash="c" * 64,
                upstream_item_ids=[],
            )
            db.add_all([self.input_item, self.unusable_item, self.other_item])
            db.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    def _task(self, key="spec", **overrides) -> TaskCreate:
        values = {
            "task_key": key,
            "recipient": TaskRecipient.PRODUCT_MANAGER,
            "title": "整理需求",
            "instructions": "基于指定需求整理用户行为和验收条件",
            "expected_output_type": ConfigurationItemType.APP_SPEC,
            "input_configuration_item_ids": [self.input_item.item_id],
        }
        return TaskCreate.model_validate({**values, **overrides})

    def _payload(self, *, version=1, tasks=None, message_id=None) -> PlanCreate:
        return PlanCreate(
            version=version,
            cause_message_id=message_id or self.message.id,
            tasks=tasks if tasks is not None else [self._task()],
        )

    def _save(self, db, payload=None, **overrides) -> Plan:
        return plan_service.save_plan(
            db,
            overrides.get("user", self.owner),
            overrides.get("project_id", self.project.id),
            overrides.get("run_id", self.run.run_id),
            payload or self._payload(),
        )

    def _tasks(self, db, plan_id) -> list[Task]:
        return task_service.list_user_plan_tasks(db, self.owner, self.project.id, plan_id)

    def test_saves_complete_plan_with_stable_ids_and_forward_dependencies(self) -> None:
        # 展示顺序可以与拓扑顺序不同；依赖不能靠 list 的先后来猜。
        payload = self._payload(
            tasks=[
                self._task(
                    "design",
                    recipient="SolutionArchitect",
                    expected_output_type="system_design",
                    input_configuration_item_ids=[],
                    depends_on_task_keys=["spec"],
                ),
                self._task(),
            ]
        )
        with self.session_factory() as db, patch("app.core.llm.chat_completion") as llm:
            plan = self._save(db, payload)
            tasks = self._tasks(db, plan.plan_id)
            self.assertEqual(plan.project_id, self.project.id)
            self.assertEqual(plan.build_run_id, self.run.run_id)
            self.assertEqual(plan.cause_message_id, self.message.id)
            self.assertEqual(plan.status, PlanStatus.PENDING.value)
            self.assertTrue(plan.plan_id.startswith("plan_"))
            self.assertEqual(len(plan.definition_hash), 64)
            self.assertEqual([task.task_key for task in tasks], ["design", "spec"])
            self.assertEqual([task.position for task in tasks], [1, 2])
            self.assertEqual(tasks[0].depends_on_task_ids, [tasks[1].task_id])
            self.assertEqual(tasks[1].input_configuration_item_ids, [self.input_item.item_id])
            self.assertTrue(all(task.status == TaskStatus.PENDING.value for task in tasks))
            self.assertTrue(all(task.task_id.startswith("task_") for task in tasks))
            self.assertEqual(PlanOut.model_validate(plan).version, 1)
            self.assertEqual(
                TaskOut.model_validate(tasks[0]).recipient, TaskRecipient.SOLUTION_ARCHITECT
            )
            llm.assert_not_called()
            self.assertEqual(db.get(BuildRun, self.run.id).status, BuildRunStatus.QUEUED.value)
            self.assertEqual(db.get(Project, self.project.id).status, "available")
            self.assertEqual(db.scalar(select(func.count()).select_from(ConfigurationItem)), 3)
            self.assertEqual(db.scalar(select(func.count()).select_from(ProjectMessage)), 3)

    def test_queries_survive_new_session_and_keep_project_scope(self) -> None:
        with self.session_factory() as db:
            plan = self._save(db)
            task_id = self._tasks(db, plan.plan_id)[0].task_id
        with self.session_factory() as db:
            loaded = plan_service.get_user_plan(db, self.owner, self.project.id, plan.plan_id)
            task = task_service.get_user_task(db, self.owner, self.project.id, task_id)
            self.assertEqual(loaded.plan_id, plan.plan_id)
            self.assertEqual(task.plan_id, plan.plan_id)
            self.assertEqual(self._tasks(db, plan.plan_id)[0].task_id, task_id)
            for reader, identity in (
                (plan_service.get_user_plan, plan.plan_id),
                (task_service.list_user_plan_tasks, plan.plan_id),
                (task_service.get_user_task, task_id),
            ):
                with self.subTest(reader=reader.__name__):
                    with self.assertRaises(NotFoundException):
                        reader(db, self.outsider, self.project.id, identity)
                    with self.assertRaises(NotFoundException):
                        reader(db, self.owner, self.other_project.id, identity)
                    with self.assertRaises(NotFoundException):
                        reader(db, self.owner, self.project.id, "missing")
            with self.assertRaises(NotFoundException):
                plan_service.list_user_plans(db, self.outsider, self.project.id, self.run.run_id)
            with self.assertRaises(NotFoundException):
                plan_service.list_user_plans(db, self.owner, self.project.id, self.other_run.run_id)

    def test_replay_returns_original_ids_even_after_run_finishes(self) -> None:
        with self.session_factory() as db:
            payload = self._payload()
            first = self._save(db, payload)
            first_task = self._tasks(db, first.plan_id)[0]
            replay = self._save(db, payload)
            self.assertEqual(first.plan_id, replay.plan_id)
            run = db.get(BuildRun, self.run.id)
            run.status = BuildRunStatus.FAILED.value
            run.active_slot = None
            item = db.get(ConfigurationItem, self.input_item.id)
            item.state = "unusable"
            item.unusable_at = datetime(2026, 9, 4)
            item.unusable_reason = "requirements changed"
            db.commit()
            replay = self._save(db, payload)
            self.assertEqual(first.plan_id, replay.plan_id)
            self.assertEqual(self._tasks(db, first.plan_id)[0].task_id, first_task.task_id)
            self.assertEqual(db.scalar(select(func.count()).select_from(Plan)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(Task)), 1)

    def test_same_version_with_different_definition_conflicts(self) -> None:
        with self.session_factory() as db:
            original = self._save(db)
            for changed in (
                self._task(title="Different title"),
                self._task(input_configuration_item_ids=[]),
                self._task(recipient="SoftwareEngineer"),
            ):
                with self.subTest(task=changed), self.assertRaises(ConflictException):
                    self._save(db, self._payload(tasks=[changed]))
            self.assertEqual(self._tasks(db, original.plan_id)[0].title, "整理需求")
            self.assertEqual(db.scalar(select(func.count()).select_from(Plan)), 1)

    def test_new_version_preserves_previous_plan_and_task_definitions(self) -> None:
        with self.session_factory() as db:
            old = self._save(db)
            old_task = self._tasks(db, old.plan_id)[0]
            new = self._save(db, self._payload(version=2, tasks=[self._task(title="新计划")]))
            plans = plan_service.list_user_plans(db, self.owner, self.project.id, self.run.run_id)
            self.assertEqual([plan.version for plan in plans], [1, 2])
            self.assertNotEqual(old.plan_id, new.plan_id)
            self.assertNotEqual(old_task.task_id, self._tasks(db, new.plan_id)[0].task_id)
            self.assertEqual(old_task.title, "整理需求")
            self.assertEqual(old_task.status, "pending")
            self.assertEqual(old.status, "pending")

    def test_rejects_foreign_run_or_message_and_non_user_cause(self) -> None:
        invalid = [
            ({"user": self.outsider}, self._payload(), NotFoundException),
            ({"project_id": 999999}, self._payload(), NotFoundException),
            ({"run_id": self.other_run.run_id}, self._payload(), NotFoundException),
            ({"run_id": "run_missing"}, self._payload(), NotFoundException),
            ({}, self._payload(message_id=self.other_message.id), NotFoundException),
            ({}, self._payload(message_id=999999), NotFoundException),
            ({}, self._payload(message_id=self.assistant_message.id), BusinessException),
            ({"run_id": self.history_run.run_id}, self._payload(), ConflictException),
        ]
        for kwargs, payload, error in invalid:
            with self.subTest(kwargs=kwargs, message=payload.cause_message_id):
                with self.session_factory() as db, self.assertRaises(error):
                    self._save(db, payload, **kwargs)
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Plan)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(Task)), 0)

    def test_validates_exact_input_ids_and_never_uses_newest_item(self) -> None:
        for item_id, error in (
            ("ci_missing", NotFoundException),
            (self.other_item.item_id, NotFoundException),
            (self.unusable_item.item_id, ConflictException),
        ):
            with self.subTest(item_id=item_id):
                payload = self._payload(tasks=[self._task(input_configuration_item_ids=[item_id])])
                with self.session_factory() as db, self.assertRaises(error):
                    self._save(db, payload)
        with self.session_factory() as db:
            plan = self._save(db)
            # 同项目 app_spec v2 已存在且不可用，任务仍准确引用可用的 v1。
            self.assertEqual(
                self._tasks(db, plan.plan_id)[0].input_configuration_item_ids, ["ci_base"]
            )

    def test_rechecks_cached_run_and_input_state_before_save(self) -> None:
        with self.session_factory() as db:
            cached_run = db.get(BuildRun, self.run.id)
            cached_item = db.get(ConfigurationItem, self.input_item.id)
            with self.session_factory() as writer:
                item = writer.get(ConfigurationItem, self.input_item.id)
                item.state = "unusable"
                item.unusable_reason = "new requirement"
                item.unusable_at = datetime(2026, 9, 4)
                writer.commit()
            self.assertEqual(cached_item.state, "usable")
            with self.assertRaises(ConflictException):
                self._save(db)
            self.assertEqual(cached_item.state, "unusable")
            # 保存失败会回滚并过期缓存；重新装载后再模拟另一个会话结束运行。
            self.assertEqual(cached_run.status, "queued")
            with self.session_factory() as writer:
                run = writer.get(BuildRun, self.run.id)
                run.status = "failed"
                run.active_slot = None
                writer.commit()
            self.assertEqual(cached_run.status, "queued")
            with self.assertRaisesRegex(ConflictException, "活动 BuildRun"):
                self._save(db, self._payload(tasks=[self._task(input_configuration_item_ids=[])]))

    def test_schema_rejects_invalid_task_graphs(self) -> None:
        invalid_graphs = (
            [],
            [self._task(), self._task()],
            [self._task(depends_on_task_keys=["spec"])],
            [self._task(depends_on_task_keys=["from_another_plan"])],
            [
                self._task("a", depends_on_task_keys=["b"]),
                self._task("b", depends_on_task_keys=["a"]),
            ],
            [
                self._task("a", depends_on_task_keys=["b"]),
                self._task("b", depends_on_task_keys=["c"]),
                self._task("c", depends_on_task_keys=["a"]),
            ],
        )
        for tasks in invalid_graphs:
            with self.subTest(tasks=tasks), self.assertRaises(ValidationError):
                self._payload(tasks=tasks)

    def test_schema_normalizes_text_and_rejects_bad_fields(self) -> None:
        normalized = self._task(
            "  spec  ", title="  标题  ", input_configuration_item_ids=[" ci_base "]
        )
        self.assertEqual(normalized.task_key, "spec")
        self.assertEqual(normalized.title, "标题")
        self.assertEqual(normalized.input_configuration_item_ids, ["ci_base"])
        for fields in (
            {"title": "  "},
            {"instructions": "  "},
            {"task_key": "UPPERCASE"},
            {"recipient": "anyone"},
            {"expected_output_type": "unknown"},
            {"status": "succeeded"},
            {"depends_on_task_keys": ["a", "a"]},
            {"input_configuration_item_ids": ["ci_base", " ci_base "]},
            {"input_configuration_item_ids": [" "]},
            {"input_configuration_item_ids": ["x" * 41]},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                self._task(**fields)
        with self.assertRaises(ValidationError):
            self._payload(version=0)
        with self.assertRaises(ValidationError):
            PlanCreate.model_validate({**self._payload().model_dump(), "status": "succeeded"})

    def test_mutated_submission_is_revalidated_before_persistence(self) -> None:
        payload = self._payload()
        payload.tasks[0].depends_on_task_keys.append("missing")
        with self.session_factory() as db, self.assertRaises(ValidationError):
            self._save(db, payload)

    def test_plan_and_task_definitions_cannot_be_overwritten(self) -> None:
        with self.session_factory() as db:
            plan = self._save(db)
            task_id = self._tasks(db, plan.plan_id)[0].id
        for model, row_id, field, value in (
            (Plan, plan.id, "version", 2),
            (Plan, plan.id, "build_run_id", self.other_run.run_id),
            (Plan, plan.id, "cause_message_id", self.other_message.id),
            (Plan, plan.id, "definition_hash", "0" * 64),
            (Task, task_id, "recipient", "QAEngineer"),
            (Task, task_id, "instructions", "changed"),
            (Task, task_id, "input_configuration_item_ids", []),
            (Task, task_id, "depends_on_task_ids", ["task_missing"]),
        ):
            with self.subTest(field=field), self.session_factory() as db:
                row = db.get(model, row_id)
                setattr(row, field, value)
                with self.assertRaisesRegex(ValueError, "不可修改"):
                    db.commit()
                db.rollback()
        for field in ("input_configuration_item_ids", "depends_on_task_ids"):
            with self.subTest(in_place=field), self.session_factory() as db:
                task = db.get(Task, task_id)
                getattr(task, field).append("changed")
                with self.assertRaisesRegex(ValueError, "不可修改"):
                    db.commit()
                db.rollback()

    def test_database_checks_plan_and_task_constraints(self) -> None:
        with self.session_factory() as db:
            plan = self._save(db)
            task = self._tasks(db, plan.plan_id)[0]
        plan_values = {
            "plan_id": "plan_new",
            "project_id": self.project.id,
            "build_run_id": self.run.run_id,
            "version": 2,
            "cause_message_id": self.message.id,
            "definition_hash": "f" * 64,
        }
        for changed in (
            {"version": 0},
            {"version": 1},
            {"plan_id": plan.plan_id},
            {"status": "unknown"},
            {"project_id": 999999},
            {"build_run_id": "run_missing"},
            {"cause_message_id": 999999},
        ):
            with self.subTest(plan=changed), self.session_factory() as db:
                db.add(Plan(**{**plan_values, **changed}))
                with self.assertRaises(IntegrityError):
                    db.commit()
                db.rollback()
        task_values = {
            "task_id": "task_new",
            "plan_id": plan.plan_id,
            "task_key": "another",
            "position": 2,
            "recipient": "ProductManager",
            "title": "Another",
            "instructions": "Do the task",
            "expected_output_type": "app_spec",
        }
        for changed in (
            {"task_id": task.task_id},
            {"task_key": task.task_key},
            {"position": 1},
            {"position": 0},
            {"plan_id": "plan_missing"},
            {"recipient": "unknown"},
            {"expected_output_type": "unknown"},
            {"status": "unknown"},
        ):
            with self.subTest(task=changed), self.session_factory() as db:
                db.add(Task(**{**task_values, **changed}))
                with self.assertRaises(IntegrityError):
                    db.commit()
                db.rollback()

    def test_failed_task_insert_rolls_back_parent_and_all_siblings(self) -> None:
        payload = self._payload(tasks=[self._task("one"), self._task("two")])
        # plan_id、两个 task_id 的 UUID 相同：Task 唯一约束失败，Plan 不能单独留下。
        with self.session_factory() as db, patch.object(plan_service, "uuid4") as uuid:
            uuid.return_value.hex = "a" * 32
            with self.assertRaises(ConflictException):
                self._save(db, payload)
            self.assertEqual(db.scalar(select(func.count()).select_from(Plan)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(Task)), 0)

    def test_unexpected_commit_failure_rolls_back(self) -> None:
        with self.session_factory() as db:
            with patch.object(db, "commit", side_effect=RuntimeError("write failure")):
                with self.assertRaisesRegex(RuntimeError, "write failure"):
                    self._save(db)
            self.assertEqual(db.scalar(select(func.count()).select_from(Plan)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(Task)), 0)

    def test_integrity_race_returns_winner_or_reports_definition_conflict(self) -> None:
        with self.session_factory() as db:
            winner = self._save(db)
        for payload, should_conflict in (
            (self._payload(), False),
            (self._payload(tasks=[self._task(title="different")]), True),
        ):
            with self.subTest(conflict=should_conflict), self.session_factory() as db:
                # 模拟首次查询没看到另一个事务，但数据库唯一约束已能看到赢家。
                original = plan_service._find_version
                calls = 0

                def hide_before_insert(*args, finder=original, **kwargs):
                    nonlocal calls
                    calls += 1
                    return None if calls <= 2 else finder(*args, **kwargs)

                with patch.object(plan_service, "_find_version", side_effect=hide_before_insert):
                    if should_conflict:
                        with self.assertRaises(ConflictException):
                            self._save(db, payload)
                    else:
                        self.assertEqual(self._save(db, payload).plan_id, winner.plan_id)
                self.assertEqual(db.scalar(select(func.count()).select_from(Plan)), 1)
                self.assertEqual(db.scalar(select(func.count()).select_from(Task)), 1)

    def test_replay_rechecks_version_after_project_lock_before_validating_terminal_run(
        self,
    ) -> None:
        with self.session_factory() as db:
            winner = self._save(db)
            run = db.get(BuildRun, self.run.id)
            run.status = "failed"
            run.active_slot = None
            db.commit()
        with self.session_factory() as db:
            original = plan_service._find_version

            def hide_snapshot(*args, lock=False):
                return original(*args, lock=True) if lock else None

            with patch.object(plan_service, "_find_version", side_effect=hide_snapshot):
                self.assertEqual(self._save(db).plan_id, winner.plan_id)

    def _assert_delete_cascades(self, parent, row_id: int) -> None:
        with self.session_factory() as db:
            plan = self._save(db)
            task = self._tasks(db, plan.plan_id)[0]
            db.delete(db.get(parent, row_id))
            db.commit()
        with self.session_factory() as db:
            self.assertIsNone(db.get(Plan, plan.id))
            self.assertIsNone(db.get(Task, task.id))
            self.assertIsNotNone(db.get(Project, self.other_project.id))

    def test_project_deletion_cascades_without_touching_other_projects(self) -> None:
        self._assert_delete_cascades(Project, self.project.id)

    def test_run_deletion_cascades_without_touching_other_projects(self) -> None:
        self._assert_delete_cascades(BuildRun, self.run.id)

    def test_migration_matches_models_and_preserves_existing_rows(self) -> None:
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "9b3e5f7a1c2d_create_plan_and_task.py"
        )
        spec = importlib.util.spec_from_file_location("plan_task_migration", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        self.assertEqual(migration.down_revision, "7c1f3e9a4d2b")
        # 只移除新增空表，保留夹具中的现有用户、项目、消息和正式成果。
        with self.engine.begin() as connection:
            Task.__table__.drop(connection)
            Plan.__table__.drop(connection)
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            self.assertEqual(
                compare_metadata(MigrationContext.configure(connection), Base.metadata), []
            )
        with self.session_factory() as db:
            plan = self._save(db)
            self.assertEqual(self._tasks(db, plan.plan_id)[0].status, "pending")
        with self.engine.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.downgrade()
            self.assertNotIn("plan", inspect(connection).get_table_names())
            self.assertNotIn("task", inspect(connection).get_table_names())
            self.assertEqual(connection.scalar(select(func.count()).select_from(ProjectMessage)), 3)
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(ConfigurationItem)), 3
            )
            migration.upgrade()
            self.assertEqual(
                compare_metadata(MigrationContext.configure(connection), Base.metadata), []
            )

    def test_two_concurrent_saves_create_one_plan_and_one_set_of_tasks(self) -> None:
        with TemporaryDirectory(prefix="forgeai-plan-test-") as directory:
            engine = create_engine(
                f"sqlite+pysqlite:///{Path(directory) / 'plans.db'}",
                connect_args={"check_same_thread": False, "timeout": 10},
            )
            event.listen(engine, "connect", enable_foreign_keys)
            Base.metadata.create_all(engine)
            factory = sessionmaker(bind=engine, expire_on_commit=False)
            try:
                with factory() as db:
                    owner = User(username="race", email="race@test.com", hashed_password="unused")
                    db.add(owner)
                    db.flush()
                    project = Project(user_id=owner.id, name="Race")
                    db.add(project)
                    db.flush()
                    run = BuildRun(project_id=project.id, run_id="run_race")
                    message = ProjectMessage(
                        project_id=project.id,
                        sequence=1,
                        sender="user",
                        content="Build",
                        client_message_id="race",
                    )
                    db.add_all([run, message])
                    db.commit()
                    project_id, message_id = project.id, message.id
                barrier = Barrier(2)

                def save_once():
                    payload = self._payload(
                        message_id=message_id,
                        tasks=[self._task(input_configuration_item_ids=[])],
                    )
                    with factory() as db:
                        barrier.wait(timeout=10)
                        return plan_service.save_plan(
                            db, owner, project_id, "run_race", payload
                        ).plan_id

                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(save_once)
                    second = executor.submit(save_once)
                    self.assertEqual(first.result(timeout=20), second.result(timeout=20))
                with factory() as db:
                    self.assertEqual(db.scalar(select(func.count()).select_from(Plan)), 1)
                    self.assertEqual(db.scalar(select(func.count()).select_from(Task)), 1)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
