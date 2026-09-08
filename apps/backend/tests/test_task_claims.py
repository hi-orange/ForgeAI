import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.dml import Update

from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.db.database import Base
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.project import Project
from app.models.project_message import ProjectMessage
from app.models.project_message_classification import ProjectMessageClassification
from app.models.task import Task
from app.models.user import User
from app.schemas.plan import PlanCreate, PlanOut
from app.schemas.task import TaskCreate, TaskOut
from app.services import plan as plan_service
from app.services import project_manager as project_manager_service
from app.services import task as task_service


class TaskClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory(prefix="forgeai-task-claim-test-"))
        self.engine = create_engine(
            f"sqlite+pysqlite:///{Path(directory) / 'claims.db'}",
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
                side_effect=AssertionError("领取任务不能调用模型"),
            )
        )
        with self.session_factory() as db:
            self.owner = User(username="owner", email="owner@test.com", hashed_password="unused")
            self.outsider = User(
                username="outsider", email="outsider@test.com", hashed_password="unused"
            )
            db.add_all([self.owner, self.outsider])
            db.flush()
            self.project = Project(user_id=self.owner.id, name="Demo")
            self.other_project = Project(user_id=self.owner.id, name="Other")
            db.add_all([self.project, self.other_project])
            db.flush()
            self.run = BuildRun(project_id=self.project.id, run_id="run_initial")
            self.other_run = BuildRun(project_id=self.other_project.id, run_id="run_other")
            self.message = ProjectMessage(
                project_id=self.project.id,
                sequence=1,
                sender="user",
                content="做一个读书记录应用",
                client_message_id="initial",
            )
            self.other_message = ProjectMessage(
                project_id=self.other_project.id,
                sequence=1,
                sender="user",
                content="另一个项目",
                client_message_id="initial",
            )
            db.add_all([self.run, self.other_run, self.message, self.other_message])
            db.flush()
            db.add_all(
                ProjectMessageClassification(
                    message_id=message.id,
                    category="product_change",
                    decision_summary="首次提出应用需求",
                    classifier_model="test-model",
                    prompt_version="test-v1",
                )
                for message in (self.message, self.other_message)
            )
            db.commit()
            self.plan = project_manager_service.create_initial_plan(
                db, self.owner, self.project.id, self.run.run_id, self.message.id
            )
            other_plan = project_manager_service.create_initial_plan(
                db, self.owner, self.other_project.id, self.other_run.run_id, self.other_message.id
            )
            self.task = self._tasks(db, self.plan)[0]
            self.other_task = self._tasks(db, other_plan)[0]

    def _tasks(self, db, plan) -> list[Task]:
        return task_service.list_user_plan_tasks(db, self.owner, plan.project_id, plan.plan_id)

    def _claim(self, db, **overrides) -> Task:
        return task_service.claim_product_manager_task(
            db,
            overrides.get("user", self.owner),
            overrides.get("project_id", self.project.id),
            overrides.get("run_id", self.run.run_id),
            overrides.get("task_id", self.task.task_id),
        )

    def _assert_states(self, *, run="queued", plan="pending", task="pending", stage=None) -> None:
        with self.session_factory() as db:
            saved_run = db.get(BuildRun, self.run.id)
            self.assertEqual((saved_run.status, saved_run.stage), (run, stage))
            self.assertEqual(db.get(Plan, self.plan.id).status, plan)
            self.assertEqual(db.get(Task, self.task.id).status, task)

    @staticmethod
    def _task_definition(key="requirements", **overrides) -> TaskCreate:
        return TaskCreate.model_validate(
            {
                "task_key": key,
                "recipient": "ProductManager",
                "title": "整理需求",
                "instructions": "只使用计划记录的原始需求",
                "expected_output_type": "app_spec",
                **overrides,
            }
        )

    def _custom_plan(self, db, *, tasks=None) -> Plan:
        return plan_service.save_plan(
            db,
            self.owner,
            self.project.id,
            self.run.run_id,
            PlanCreate(
                version=2,
                cause_message_id=self.message.id,
                tasks=tasks if tasks is not None else [self._task_definition()],
            ),
        )

    def test_claim_starts_task_plan_and_run_without_changing_definitions_or_other_project(self):
        with self.session_factory() as db:
            plan_before = PlanOut.model_validate(db.get(Plan, self.plan.id)).model_dump(
                exclude={"status", "updated_at"}
            )
            task_before = TaskOut.model_validate(db.get(Task, self.task.id)).model_dump(
                exclude={"status", "updated_at"}
            )
            claimed = self._claim(db)
            self.assertEqual(claimed.task_id, self.task.task_id)
            self.assertEqual(claimed.status, "running")
            self.assertEqual(
                TaskOut.model_validate(claimed).model_dump(exclude={"status", "updated_at"}),
                task_before,
            )
            self.assertEqual(
                PlanOut.model_validate(db.get(Plan, self.plan.id)).model_dump(
                    exclude={"status", "updated_at"}
                ),
                plan_before,
            )
            self.assertEqual(db.get(BuildRun, self.run.id).active_slot, 1)
            self.assertEqual(db.get(Project, self.project.id).status, "draft")
            self.assertEqual(db.get(BuildRun, self.other_run.id).status, "queued")
            self.assertEqual(db.get(Task, self.other_task.id).status, "pending")
            self.assertEqual(db.scalar(select(func.count()).select_from(ConfigurationItem)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(ProjectMessage)), 2)
        self._assert_states(run="running", plan="running", task="running", stage="pm")
        self.llm.assert_not_called()

    def test_repeated_claim_is_a_conflict_not_successful_replay(self):
        with self.session_factory() as db:
            self._claim(db)
        with self.session_factory() as db:
            with self.assertRaisesRegex(ConflictException, "不能重复领取"):
                self._claim(db)
            self.assertFalse(db.in_transaction())
        self._assert_states(run="running", plan="running", task="running", stage="pm")

    def test_owner_project_run_and_task_scope_and_unknown_ids(self):
        for overrides in (
            {"user": self.outsider},
            {"project_id": 999999},
            {"run_id": "run_missing"},
            {"run_id": self.other_run.run_id},
            {"task_id": "task_missing"},
            {"task_id": self.other_task.task_id},
        ):
            with self.subTest(overrides=overrides), self.session_factory() as db:
                with self.assertRaises(NotFoundException):
                    self._claim(db, **overrides)
                self.assertFalse(db.in_transaction())
                self._assert_states()

    def test_cannot_use_old_task_with_a_new_run_in_the_same_project(self):
        with self.session_factory() as db:
            old_run = db.get(BuildRun, self.run.id)
            old_run.status = "failed"
            old_run.active_slot = None
            db.commit()
            new_run = BuildRun(project_id=self.project.id, run_id="run_new")
            db.add(new_run)
            db.commit()
            with self.assertRaises(NotFoundException):
                self._claim(db, run_id=new_run.run_id)
            self.assertEqual(db.get(BuildRun, new_run.id).status, "queued")
        self._assert_states(run="failed")

    def test_finished_runs_cannot_be_restarted(self):
        for status in ("succeeded", "failed"):
            with self.subTest(status=status), self.session_factory() as db:
                run = db.get(BuildRun, self.run.id)
                run.status = status
                run.active_slot = None
                db.commit()
                with self.assertRaisesRegex(ConflictException, "构建任务已结束"):
                    self._claim(db)
                self._assert_states(run=status)

    def test_other_running_stages_cannot_be_reset_to_pm(self):
        for stage in ("architect", "developer", "qa", "runtime"):
            with self.subTest(stage=stage), self.session_factory() as db:
                run = db.get(BuildRun, self.run.id)
                run.status, run.stage = "running", stage
                db.commit()
                with self.assertRaisesRegex(ConflictException, "其他阶段"):
                    self._claim(db)
                self._assert_states(run="running", stage=stage)

    def test_finished_or_cancelled_plans_cannot_be_claimed(self):
        for status in ("succeeded", "failed", "cancelled"):
            with self.subTest(status=status), self.session_factory() as db:
                db.get(Plan, self.plan.id).status = status
                db.commit()
                with self.assertRaisesRegex(ConflictException, "计划已结束"):
                    self._claim(db)
                self._assert_states(plan=status)

    def test_only_pending_tasks_can_be_claimed(self):
        for status in ("running", "succeeded", "failed", "cancelled"):
            with self.subTest(status=status), self.session_factory() as db:
                db.get(Task, self.task.id).status = status
                db.commit()
                with self.assertRaisesRegex(ConflictException, "不能重复领取"):
                    self._claim(db)
                self._assert_states(task=status)

    def test_can_claim_pending_task_in_the_same_already_running_plan(self):
        with self.session_factory() as db:
            run = db.get(BuildRun, self.run.id)
            run.status, run.stage = "running", "pm"
            db.get(Plan, self.plan.id).status = "running"
            db.commit()
            self._claim(db)
        self._assert_states(run="running", plan="running", task="running", stage="pm")

    def test_rejects_other_roles_and_wrong_output_type(self):
        for overrides in (
            {"recipient": "SolutionArchitect"},
            {"recipient": "SoftwareEngineer"},
            {"recipient": "QAEngineer"},
            {"expected_output_type": "system_design"},
        ):
            with self.subTest(overrides=overrides), self.session_factory() as db:
                plan = self._custom_plan(db, tasks=[self._task_definition(**overrides)])
                task = self._tasks(db, plan)[0]
                with self.assertRaises(BusinessException):
                    self._claim(db, task_id=task.task_id)
                self.assertEqual(db.get(Plan, plan.id).status, "pending")
                self.assertEqual(db.get(Task, task.id).status, "pending")
                self._assert_states()
                db.delete(plan)
                db.commit()

    def test_dependency_tasks_are_not_claimed_even_when_upstream_status_says_succeeded(self):
        with self.session_factory() as db:
            plan = self._custom_plan(
                db,
                tasks=[
                    self._task_definition("first"),
                    self._task_definition("second", depends_on_task_keys=["first"]),
                ],
            )
            upstream, dependent = self._tasks(db, plan)
            for status in ("pending", "succeeded"):
                with self.subTest(upstream_status=status):
                    upstream.status = status
                    db.commit()
                    with self.assertRaisesRegex(BusinessException, "无任务依赖"):
                        self._claim(db, task_id=dependent.task_id)
                    self.assertEqual(db.get(Task, dependent.id).status, "pending")
                    self._assert_states()

    def test_tasks_with_configuration_inputs_are_explicitly_out_of_scope(self):
        with self.session_factory() as db:
            item = ConfigurationItem(
                item_id="ci_input",
                project_id=self.project.id,
                producer_run_id=self.run.run_id,
                semantic_type="app_spec",
                version=1,
                payload={"goal": "Existing intent"},
                content_hash="a" * 64,
                upstream_item_ids=[],
            )
            db.add(item)
            db.commit()
            plan = self._custom_plan(
                db, tasks=[self._task_definition(input_configuration_item_ids=[item.item_id])]
            )
            task = self._tasks(db, plan)[0]
            with self.assertRaisesRegex(BusinessException, "关联不正确"):
                self._claim(db, task_id=task.task_id)
            self.assertEqual(db.get(Task, task.id).input_configuration_item_ids, [item.item_id])
            self._assert_states()

    def test_claims_explicit_task_not_latest_plan_and_does_not_mix_running_plans(self):
        with self.session_factory() as db:
            newer = self._custom_plan(db)
            newer_task = self._tasks(db, newer)[0]
            claimed = self._claim(db)
            self.assertEqual(claimed.plan_id, self.plan.plan_id)
            with self.assertRaisesRegex(ConflictException, "另一份计划"):
                self._claim(db, task_id=newer_task.task_id)
            self.assertEqual(db.get(Plan, newer.id).status, "pending")
            self.assertEqual(db.get(Task, newer_task.id).status, "pending")
        self._assert_states(run="running", plan="running", task="running", stage="pm")

    def test_new_messages_do_not_change_the_claimed_requirement_source(self):
        with self.session_factory() as db:
            later = ProjectMessage(
                project_id=self.project.id,
                sequence=2,
                sender="user",
                content="后来要求改成电影记录应用",
                client_message_id="later",
            )
            db.add(later)
            db.commit()
            claimed = self._claim(db)
            plan = plan_service.get_user_plan(db, self.owner, self.project.id, claimed.plan_id)
            self.assertEqual(plan.cause_message_id, self.message.id)
            self.assertNotEqual(plan.cause_message_id, later.id)
            self.assertEqual(
                db.get(ProjectMessage, plan.cause_message_id).content, self.message.content
            )

    def test_rechecks_stale_orm_task_plan_and_run_status(self):
        for model, row_id, changes, error in (
            (Task, self.task.id, {"status": "cancelled"}, "不能重复领取"),
            (Plan, self.plan.id, {"status": "cancelled"}, "计划已结束"),
            (BuildRun, self.run.id, {"status": "failed", "active_slot": None}, "构建任务已结束"),
        ):
            with self.subTest(model=model.__name__), self.session_factory() as db:
                cached = db.get(model, row_id)
                original = {field: getattr(cached, field) for field in changes}
                with self.session_factory() as writer:
                    row = writer.get(model, row_id)
                    for field, value in changes.items():
                        setattr(row, field, value)
                    writer.commit()
                self.assertEqual(cached.status, original["status"])
                with self.assertRaisesRegex(ConflictException, error):
                    self._claim(db)
                with self.session_factory() as writer:
                    row = writer.get(model, row_id)
                    for field, value in original.items():
                        setattr(row, field, value)
                    writer.commit()
                self._assert_states()

    def test_plan_write_failure_rolls_back_already_updated_run_and_task(self):
        def fail_plan_update(_mapper, _connection, _target):
            raise RuntimeError("plan update failed")

        event.listen(Plan, "before_update", fail_plan_update)
        try:
            with self.session_factory() as db:
                with self.assertRaisesRegex(RuntimeError, "plan update failed"):
                    self._claim(db)
                self.assertFalse(db.in_transaction())
        finally:
            event.remove(Plan, "before_update", fail_plan_update)
        self._assert_states()
        with self.session_factory() as db:
            self._claim(db)
        self._assert_states(run="running", plan="running", task="running", stage="pm")

    def test_commit_failure_rolls_back_all_three_states(self):
        with self.session_factory() as db:
            with patch.object(db, "commit", side_effect=RuntimeError("commit failed")):
                with self.assertRaisesRegex(RuntimeError, "commit failed"):
                    self._claim(db)
            self.assertFalse(db.in_transaction())
        self._assert_states()

    def test_lost_task_conditional_update_rolls_back_run_reservation(self):
        with self.session_factory() as db:
            connection = db.connection()
            original_execute = connection.execute

            def lose_task_update(statement, *args, **kwargs):
                if isinstance(statement, Update) and statement.table.name == "task":
                    return SimpleNamespace(rowcount=0)
                return original_execute(statement, *args, **kwargs)

            with patch.object(connection, "execute", side_effect=lose_task_update):
                with self.assertRaisesRegex(ConflictException, "其他请求领取"):
                    self._claim(db)
        self._assert_states()

    def _concurrent_claims(self, first_task_id, second_task_id):
        # 强制两个独立会话都先读到运行状态，再竞争条件更新。
        barrier = Barrier(2)

        def wait_before_run_update(_connection, _cursor, statement, _params, _context, _many):
            if statement.startswith("UPDATE build_run "):
                barrier.wait(timeout=10)

        def claim_once(task_id):
            with self.session_factory() as db:
                try:
                    return self._claim(db, task_id=task_id).task_id
                except ConflictException:
                    return None

        event.listen(self.engine, "before_cursor_execute", wait_before_run_update)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(claim_once, first_task_id)
                second = executor.submit(claim_once, second_task_id)
                results = [first.result(timeout=20), second.result(timeout=20)]
        finally:
            event.remove(self.engine, "before_cursor_execute", wait_before_run_update)
        self.assertEqual(results.count(None), 1)
        return results

    def test_concurrent_claims_of_same_pending_task_have_exactly_one_winner(self):
        results = self._concurrent_claims(self.task.task_id, self.task.task_id)
        self.assertIn(self.task.task_id, results)
        self._assert_states(run="running", plan="running", task="running", stage="pm")

    def test_concurrent_claims_do_not_activate_different_plans_in_queued_run(self):
        with self.session_factory() as db:
            newer = self._custom_plan(db)
            newer_task = self._tasks(db, newer)[0]
        self._concurrent_claims(self.task.task_id, newer_task.task_id)
        with self.session_factory() as db:
            plans = plan_service.list_user_plans(db, self.owner, self.project.id, self.run.run_id)
            self.assertEqual(sorted(plan.status for plan in plans), ["pending", "running"])
            self.assertEqual(
                sorted(self._tasks(db, plan)[0].status for plan in plans), ["pending", "running"]
            )

    def test_concurrent_claims_do_not_activate_different_plans_in_already_running_run(self):
        with self.session_factory() as db:
            newer = self._custom_plan(db)
            newer_task = self._tasks(db, newer)[0]
            run = db.get(BuildRun, self.run.id)
            run.status, run.stage = "running", "pm"
            db.commit()
        self._concurrent_claims(self.task.task_id, newer_task.task_id)
        with self.session_factory() as db:
            plans = plan_service.list_user_plans(db, self.owner, self.project.id, self.run.run_id)
            self.assertEqual(sorted(plan.status for plan in plans), ["pending", "running"])
            self.assertEqual(
                sorted(self._tasks(db, plan)[0].status for plan in plans), ["pending", "running"]
            )


if __name__ == "__main__":
    unittest.main()
