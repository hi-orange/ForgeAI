import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.agents.prompts.project_manager import MESSAGE_CLASSIFICATION_PROMPT_VERSION
from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.db.database import Base
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.project import Project
from app.models.project_message import ProjectMessage
from app.models.project_message_classification import (
    ProjectMessageCategory,
    ProjectMessageClassification,
)
from app.models.task import Task
from app.models.user import User
from app.schemas.plan import PlanCreate
from app.schemas.project_message_classification import ProjectMessageClassificationDecision
from app.schemas.task import TaskCreate
from app.services import plan as plan_service
from app.services import project_manager as project_manager_service
from app.services import task as task_service


class ProjectManagerPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory(prefix="forgeai-initial-plan-test-"))
        self.engine = create_engine(
            f"sqlite+pysqlite:///{Path(directory) / 'planning.db'}",
            connect_args={"check_same_thread": False, "timeout": 10},
        )
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.llm = self.enterContext(
            patch(
                "app.agents.project_manager.chat_completion",
                side_effect=AssertionError("创建初始计划不应调用模型"),
            )
        )
        with self.session_factory() as db:
            self.owner = User(username="owner", email="owner@test.com", hashed_password="unused")
            self.outsider = User(
                username="outsider", email="outsider@test.com", hashed_password="unused"
            )
            db.add_all([self.owner, self.outsider])
            db.flush()
            self.project = Project(
                user_id=self.owner.id, name="读书记录", prompt="旧的初始文本，不应替代触发消息"
            )
            # 同一所有者的另一个项目也必须保持隔离。
            self.other_project = Project(user_id=self.owner.id, name="Other")
            db.add_all([self.project, self.other_project])
            db.flush()
            self.run = BuildRun(project_id=self.project.id, run_id="run_initial")
            self.other_run = BuildRun(project_id=self.other_project.id, run_id="run_other")
            self.message = self._message(self.project.id, 1, "做一个读书记录应用")
            self.later_message = self._message(self.project.id, 2, "改成电影记录应用")
            self.assistant_message = self._message(
                self.project.id, 3, "正在处理", sender="assistant"
            )
            self.other_message = self._message(self.other_project.id, 1, "另一个项目的需求")
            db.add_all(
                [
                    self.run,
                    self.other_run,
                    self.message,
                    self.later_message,
                    self.assistant_message,
                    self.other_message,
                ]
            )
            db.flush()
            db.add_all(
                self._classification(message)
                for message in (self.message, self.later_message, self.other_message)
            )
            db.commit()

    @staticmethod
    def _message(project_id, sequence, content, *, sender="user") -> ProjectMessage:
        return ProjectMessage(
            project_id=project_id,
            sequence=sequence,
            sender=sender,
            content=content,
            client_message_id=f"message-{sequence}",
        )

    @staticmethod
    def _classification(message) -> ProjectMessageClassification:
        return ProjectMessageClassification(
            message_id=message.id,
            category="product_change",
            decision_summary="用户提出应用需求",
            classifier_model="test-model",
            prompt_version=MESSAGE_CLASSIFICATION_PROMPT_VERSION,
        )

    def _create(self, db, **overrides) -> Plan:
        return project_manager_service.create_initial_plan(
            db,
            overrides.get("user", self.owner),
            overrides.get("project_id", self.project.id),
            overrides.get("run_id", self.run.run_id),
            overrides.get("message_id", self.message.id),
        )

    def _tasks(self, db, plan_id) -> list[Task]:
        return task_service.list_user_plan_tasks(db, self.owner, self.project.id, plan_id)

    def _assert_counts(self, db, plans=0, tasks=0) -> None:
        self.assertEqual(db.scalar(select(func.count()).select_from(Plan)), plans)
        self.assertEqual(db.scalar(select(func.count()).select_from(Task)), tasks)

    def _add_item(self, db, *, usable=True, other_project=False) -> ConfigurationItem:
        item = ConfigurationItem(
            item_id="ci_existing",
            project_id=self.other_project.id if other_project else self.project.id,
            producer_run_id=self.other_run.run_id if other_project else self.run.run_id,
            semantic_type="app_spec",
            version=1,
            payload={"goal": "已存在的产品意图"},
            content_hash="a" * 64,
            upstream_item_ids=[],
            state="usable" if usable else "unusable",
            unusable_reason=None if usable else "过期成果",
            unusable_at=None if usable else datetime(2026, 9, 4),
        )
        db.add(item)
        db.commit()
        return item

    def _save_custom_plan(self, db, *, version=1) -> Plan:
        return plan_service.save_plan(
            db,
            self.owner,
            self.project.id,
            self.run.run_id,
            PlanCreate(
                version=version,
                cause_message_id=self.message.id,
                tasks=[
                    TaskCreate(
                        task_key="custom",
                        recipient="ProductManager",
                        title="已安排的工作",
                        instructions="已有任务，不能被初始规划覆盖",
                        expected_output_type="app_spec",
                    )
                ],
            ),
        )

    def test_creates_one_pending_requirements_task_and_preserves_other_state(self) -> None:
        with self.session_factory() as db:
            plan = self._create(db)
            plan_id = plan.plan_id
        with self.session_factory() as db:
            saved = plan_service.get_user_plan(db, self.owner, self.project.id, plan_id)
            tasks = self._tasks(db, plan_id)
            self.assertEqual(saved.version, 1)
            self.assertEqual(saved.cause_message_id, self.message.id)
            self.assertEqual(saved.build_run_id, self.run.run_id)
            self.assertEqual(saved.project_id, self.project.id)
            self.assertEqual(saved.status, "pending")
            self.assertEqual(len(tasks), 1)
            task = tasks[0]
            self.assertEqual(task.task_key, "requirements")
            self.assertEqual(task.recipient, "ProductManager")
            self.assertEqual(task.expected_output_type, "app_spec")
            self.assertEqual(task.status, "pending")
            self.assertEqual(task.position, 1)
            self.assertEqual(task.input_configuration_item_ids, [])
            self.assertEqual(task.depends_on_task_ids, [])
            self.assertIn("cause_message_id", task.instructions)
            self.assertIn("不要使用之后的新消息", task.instructions)
            self.assertIn("不生成技术方案或代码", task.instructions)
            self.assertEqual(db.get(BuildRun, self.run.id).status, "queued")
            self.assertIsNone(db.get(BuildRun, self.run.id).stage)
            self.assertEqual(db.get(Project, self.project.id).status, "draft")
            self.assertEqual(db.scalar(select(func.count()).select_from(BuildRun)), 2)
            self.assertEqual(db.scalar(select(func.count()).select_from(ProjectMessage)), 4)
            self.assertEqual(db.scalar(select(func.count()).select_from(ConfigurationItem)), 0)
            self._assert_counts(db, 1, 1)
        self.llm.assert_not_called()

    def test_replay_after_new_messages_keeps_original_ids_definition_and_status(self) -> None:
        with self.session_factory() as db:
            plan = self._create(db)
            task = self._tasks(db, plan.plan_id)[0]
            original = (plan.plan_id, plan.definition_hash, task.task_id, task.instructions)
            # 模拟后续工作已开始、项目元数据和对话已更新。
            plan.status = "running"
            task.status = "running"
            db.get(Project, self.project.id).name = "新的项目名称"
            db.add(self._message(self.project.id, 4, "后来的需求不能混入原任务"))
            db.commit()
        with self.session_factory() as db:
            replay = self._create(db)
            task = self._tasks(db, replay.plan_id)[0]
            self.assertEqual(
                (replay.plan_id, replay.definition_hash, task.task_id, task.instructions), original
            )
            self.assertEqual(replay.cause_message_id, self.message.id)
            self.assertEqual(replay.status, "running")
            self.assertEqual(task.status, "running")
            self._assert_counts(db, 1, 1)

    def test_another_message_cannot_replace_initial_plan(self) -> None:
        with self.session_factory() as db:
            plan = self._create(db)
            with self.assertRaises(ConflictException):
                self._create(db, message_id=self.later_message.id)
            self.assertEqual(plan.cause_message_id, self.message.id)
            self._assert_counts(db, 1, 1)

    def test_requires_persisted_classification_and_does_not_classify_implicitly(self) -> None:
        with self.session_factory() as db:
            db.delete(
                db.scalar(
                    select(ProjectMessageClassification).where(
                        ProjectMessageClassification.message_id == self.message.id
                    )
                )
            )
            db.commit()
            with self.assertRaisesRegex(NotFoundException, "尚未分类"):
                self._create(db)
            self._assert_counts(db)
        self.llm.assert_not_called()

    def test_rejects_all_non_product_change_categories(self) -> None:
        for category in ("inquiry", "stop", "implementation_repair"):
            with self.subTest(category=category), self.session_factory() as db:
                classification = db.scalar(
                    select(ProjectMessageClassification).where(
                        ProjectMessageClassification.message_id == self.message.id
                    )
                )
                classification.category = category
                db.commit()
                with self.assertRaisesRegex(BusinessException, "product_change"):
                    self._create(db)
                self._assert_counts(db)
        self.llm.assert_not_called()

    def test_owner_project_run_and_message_boundaries(self) -> None:
        for overrides, error in (
            ({"user": self.outsider}, NotFoundException),
            ({"project_id": 999999}, NotFoundException),
            ({"message_id": self.other_message.id}, NotFoundException),
            ({"message_id": 999999}, NotFoundException),
            ({"message_id": self.assistant_message.id}, BusinessException),
            ({"run_id": self.other_run.run_id}, NotFoundException),
            ({"run_id": "run_missing"}, NotFoundException),
        ):
            with self.subTest(overrides=overrides), self.session_factory() as db:
                with self.assertRaises(error):
                    self._create(db, **overrides)
                self._assert_counts(db)

    def test_rejects_new_plan_for_finished_run(self) -> None:
        for status in ("succeeded", "failed"):
            with self.subTest(status=status), self.session_factory() as db:
                run = db.get(BuildRun, self.run.id)
                run.status = status
                run.active_slot = None
                db.commit()
                with self.assertRaisesRegex(ConflictException, "活动 BuildRun"):
                    self._create(db)
                self._assert_counts(db)

    def test_replay_still_works_after_run_finishes_and_outputs_exist(self) -> None:
        with self.session_factory() as db:
            plan = self._create(db)
            self._add_item(db)
            run = db.get(BuildRun, self.run.id)
            run.status = "succeeded"
            run.active_slot = None
            db.get(Project, self.project.id).status = "available"
            plan.status = "succeeded"
            self._tasks(db, plan.plan_id)[0].status = "succeeded"
            db.commit()
        with self.session_factory() as db:
            replay = self._create(db)
            self.assertEqual(replay.plan_id, plan.plan_id)
            self.assertEqual(replay.status, "succeeded")
            self.assertEqual(self._tasks(db, plan.plan_id)[0].status, "succeeded")
            self._assert_counts(db, 1, 1)

    def test_rejects_available_project_even_if_it_has_no_configuration_items(self) -> None:
        with self.session_factory() as db:
            db.get(Project, self.project.id).status = "available"
            db.commit()
            with self.assertRaisesRegex(ConflictException, "已有可用版本"):
                self._create(db)
            self._assert_counts(db)

    def test_rejects_existing_usable_or_unusable_outputs_in_draft_project(self) -> None:
        for usable in (True, False):
            with self.subTest(usable=usable), self.session_factory() as db:
                item = self._add_item(db, usable=usable)
                with self.assertRaisesRegex(ConflictException, "已有正式成果"):
                    self._create(db)
                self._assert_counts(db)
                db.delete(item)
                db.commit()

    def test_outputs_in_another_project_do_not_block_creation(self) -> None:
        with self.session_factory() as db:
            self._add_item(db, other_project=True)
            plan = self._create(db)
            self.assertEqual(self._tasks(db, plan.plan_id)[0].input_configuration_item_ids, [])
            self._assert_counts(db, 1, 1)

    def test_rechecks_cached_project_and_run_state(self) -> None:
        with self.session_factory() as db:
            project = db.get(Project, self.project.id)
            with self.session_factory() as writer:
                writer.get(Project, self.project.id).status = "available"
                writer.commit()
            self.assertEqual(project.status, "draft")
            with self.assertRaisesRegex(ConflictException, "已有可用版本"):
                self._create(db)
            self._assert_counts(db)
        with self.session_factory() as db:
            db.get(Project, self.project.id).status = "draft"
            db.commit()
            run = db.get(BuildRun, self.run.id)
            with self.session_factory() as writer:
                updated = writer.get(BuildRun, self.run.id)
                updated.status = "failed"
                updated.active_slot = None
                writer.commit()
            self.assertEqual(run.status, "queued")
            with self.assertRaisesRegex(ConflictException, "活动 BuildRun"):
                self._create(db)
            self._assert_counts(db)

    def test_does_not_overwrite_custom_plan_or_backfill_v1_behind_newer_plan(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version), self.session_factory() as db:
                existing = self._save_custom_plan(db, version=version)
                with self.assertRaises(ConflictException):
                    self._create(db)
                self.assertEqual(self._tasks(db, existing.plan_id)[0].task_key, "custom")
                self._assert_counts(db, 1, 1)
                db.delete(existing)
                db.commit()

    def test_long_requirement_is_referenced_without_truncation_or_copy_into_instructions(
        self,
    ) -> None:
        with self.session_factory() as db:
            message = self._message(self.project.id, 4, "需求" * 3999 + "结尾")
            db.add(message)
            db.flush()
            db.add(self._classification(message))
            db.commit()
            plan = self._create(db, message_id=message.id)
            source = db.get(ProjectMessage, plan.cause_message_id)
            self.assertEqual(source.content, message.content)
            self.assertEqual(len(source.content), 8000)
            self.assertLess(len(self._tasks(db, plan.plan_id)[0].instructions), 8000)

    def test_failed_task_write_rolls_back_plan_and_can_be_retried(self) -> None:
        def fail_task_insert(_mapper, _connection, _target):
            raise RuntimeError("task insert failed")

        event.listen(Task, "before_insert", fail_task_insert)
        try:
            with self.session_factory() as db:
                with self.assertRaisesRegex(RuntimeError, "task insert failed"):
                    self._create(db)
                self._assert_counts(db)
        finally:
            event.remove(Task, "before_insert", fail_task_insert)
        with self.session_factory() as db:
            self._create(db)
            self._assert_counts(db, 1, 1)

    def test_classification_stays_separate_from_planning(self) -> None:
        with self.session_factory() as db:
            message = self._message(self.project.id, 4, "做一个新的应用")
            db.add(message)
            db.commit()
            decision = ProjectMessageClassificationDecision(
                category=ProjectMessageCategory.PRODUCT_CHANGE,
                decision_summary="首次提出应用需求",
            )
            with patch.object(
                project_manager_service.project_manager_agent,
                "classify_message",
                return_value=decision,
            ) as classify:
                project_manager_service.classify_user_message(
                    db, self.owner, self.project.id, message.id
                )
                self._assert_counts(db)
                plan = self._create(db, message_id=message.id)
                self.assertEqual(plan.cause_message_id, message.id)
                self._assert_counts(db, 1, 1)
                classify.assert_called_once()

    def test_replay_releases_the_project_lock_transaction(self) -> None:
        with self.session_factory() as db:
            first = self._create(db)
        with self.session_factory() as db:
            with patch.object(db, "commit", wraps=db.commit) as commit:
                replay = self._create(db)
                self.assertEqual(replay.plan_id, first.plan_id)
                commit.assert_called_once_with()
            self.assertFalse(db.in_transaction())

    def test_concurrent_same_message_creates_only_one_plan_and_task(self) -> None:
        barrier = Barrier(2)

        def create_once():
            with self.session_factory() as db:
                barrier.wait(timeout=10)
                return self._create(db).plan_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(create_once)
            second = executor.submit(create_once)
            self.assertEqual(first.result(timeout=20), second.result(timeout=20))
        with self.session_factory() as db:
            self._assert_counts(db, 1, 1)

    def test_concurrent_different_messages_cannot_mix_requirements(self) -> None:
        barrier = Barrier(2)

        def create_once(message_id):
            with self.session_factory() as db:
                barrier.wait(timeout=10)
                try:
                    return self._create(db, message_id=message_id).cause_message_id
                except ConflictException:
                    return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(create_once, self.message.id)
            second = executor.submit(create_once, self.later_message.id)
            results = [first.result(timeout=20), second.result(timeout=20)]
        self.assertEqual(results.count(None), 1)
        with self.session_factory() as db:
            saved = db.scalar(select(Plan))
            self.assertIn(saved.cause_message_id, results)
            self._assert_counts(db, 1, 1)


if __name__ == "__main__":
    unittest.main()
