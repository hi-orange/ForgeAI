import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.agents import product_manager as product_manager_agent
from app.core.exceptions import ConflictException, NotFoundException
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
from app.orchestration import product_manager as product_manager_workflow
from app.schemas.product_manager_workflow import ProductManagerWorkflowOutcome
from app.services import project_manager as project_manager_service
from app.services import task as task_service
from app.services import task_execution


def valid_spec(*, open_questions: list[str] | None = None) -> dict[str, object]:
    return {
        "goal": "让用户记录自己的阅读进度",
        "target_users": ["个人用户"],
        "features": [
            {"id": "feat_records", "text": "新增和查看读书记录"},
            {"id": "feat_export", "text": "导出自己的记录"},
        ],
        "data_requirements": [
            {"id": "data_title", "text": "书名"},
            {"id": "data_status", "text": "阅读状态"},
        ],
        "interface_requirements": [
            {"id": "ui_list", "text": "提供读书记录列表"},
        ],
        "constraints": [
            {"id": "con_private", "text": "数据仅本人可见"},
        ],
        "acceptance_criteria": [
            {
                "id": "ac_add",
                "text": "用户可以新增记录并在列表中看到它",
                "source_ids": ["feat_records"],
            },
            {
                "id": "ac_export",
                "text": "导出后下载的文件包含用户已有的全部图书记录",
                "source_ids": ["feat_export"],
            },
        ],
        "open_questions": open_questions or [],
    }


def approval_payload(spec: dict[str, object] | None = None, **overrides) -> dict[str, object]:
    spec = spec or valid_spec()
    selected = [
        {"id": item["id"], "text": item["text"], "kind": "feature"}
        for item in spec["features"]  # type: ignore[index]
    ]
    selected.extend(
        {"id": item["id"], "text": item["text"], "kind": "data"}
        for item in spec["data_requirements"]  # type: ignore[index]
    )
    selected.extend(
        {"id": item["id"], "text": item["text"], "kind": "interface"}
        for item in spec["interface_requirements"]  # type: ignore[index]
    )
    selected.extend(
        {"id": item["id"], "text": item["text"], "kind": "constraint"}
        for item in spec["constraints"]  # type: ignore[index]
    )
    payload = {
        "client_message_id": "test-approval",
        "goal": spec["goal"],
        "selected": selected,
    }
    payload.update(overrides)
    return payload


class ProductManagerWorkflowFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory(prefix="forgeai-pm-workflow-test-"))
        self.engine = create_engine(
            f"sqlite+pysqlite:///{Path(directory) / 'workflow.db'}",
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
        self.chat = self.enterContext(
            patch.object(
                product_manager_agent,
                "chat_completion",
                return_value=json.dumps(valid_spec(), ensure_ascii=False),
            )
        )
        with self.session_factory() as db:
            self.owner = User(
                username="workflow-owner",
                email="workflow-owner@test.com",
                hashed_password="unused",
            )
            self.outsider = User(
                username="workflow-outsider",
                email="workflow-outsider@test.com",
                hashed_password="unused",
            )
            db.add_all([self.owner, self.outsider])
            db.flush()
            self.project = Project(
                user_id=self.owner.id,
                name="读书记录",
                prompt="旧字段不能替代触发消息",
            )
            db.add(self.project)
            db.flush()
            self.run = BuildRun(project_id=self.project.id, run_id="run_pm_workflow")
            self.message = ProjectMessage(
                project_id=self.project.id,
                sequence=1,
                sender="user",
                content="做一个只给自己使用、可以导出的读书记录应用",
                client_message_id="workflow-message-1",
            )
            self.later_message = ProjectMessage(
                project_id=self.project.id,
                sequence=2,
                sender="user",
                content="后来的需求不能混入当前运行",
                client_message_id="workflow-message-2",
            )
            db.add_all([self.run, self.message, self.later_message])
            db.flush()
            db.add(
                ProjectMessageClassification(
                    message_id=self.message.id,
                    category="product_change",
                    decision_summary="用户提出初始产品需求",
                    classifier_model="test-classifier",
                    prompt_version="test-classification-v1",
                )
            )
            db.commit()

    def _run(self, *, user: User | None = None, recovery_execution_id: str | None = None):
        with self.session_factory() as db:
            return product_manager_workflow.run_product_manager_workflow(
                db,
                user or self.owner,
                self.project.id,
                self.run.run_id,
                self.message.id,
                recovery_execution_id=recovery_execution_id,
            )

    def _create_plan(self) -> tuple[Plan, Task]:
        with self.session_factory() as db:
            plan = project_manager_service.create_initial_plan(
                db, self.owner, self.project.id, self.run.run_id, self.message.id
            )
            task = task_service.list_user_plan_tasks(db, self.owner, self.project.id, plan.plan_id)[
                0
            ]
            return plan, task

    def _assert_published(
        self,
        result,
        *,
        run_state: tuple[str, str | None, int | None] = ("running", "pm", 1),
    ) -> None:
        with self.session_factory() as db:
            plan = db.scalar(select(Plan).where(Plan.plan_id == result.plan_id))
            task = db.scalar(select(Task).where(Task.task_id == result.task_id))
            item = db.scalar(
                select(ConfigurationItem).where(
                    ConfigurationItem.item_id == result.configuration_item_id
                )
            )
            task_result = db.get(TaskResult, result.task_id)
            run = db.get(BuildRun, self.run.id)
            self.assertIsNotNone(plan)
            self.assertIsNotNone(task)
            self.assertIsNotNone(item)
            self.assertIsNotNone(task_result)
            self.assertEqual(plan.status, "succeeded")
            self.assertEqual(task.status, "succeeded")
            self.assertEqual(item.semantic_type, "app_spec")
            self.assertEqual(item.state, "usable")
            self.assertEqual(task_result.configuration_item_id, item.item_id)
            self.assertEqual(task_result.source_message_ids, [self.message.id])
            self.assertEqual((run.status, run.stage, run.active_slot), run_state)


class ProductManagerWorkflowTests(ProductManagerWorkflowFixture):
    def test_runs_requirements_graph_and_waits_for_approval(self):
        result = self._run()

        self.assertEqual(result.project_id, self.project.id)
        self.assertEqual(result.build_run_id, self.run.run_id)
        self.assertEqual(result.cause_message_id, self.message.id)
        self.assertEqual(result.outcome, ProductManagerWorkflowOutcome.AWAITING_APPROVAL)
        self.assertEqual(result.open_questions, [])
        self._assert_published(result)
        self.chat.assert_called_once()
        sent = json.loads(self.chat.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(sent["source_message"]["id"], self.message.id)
        self.assertNotIn(
            self.later_message.content,
            json.dumps(sent, ensure_ascii=False),
        )

    def test_open_questions_still_wait_for_checklist_approval(self):
        questions = ["导出文件需要 CSV 还是 JSON？"]
        self.chat.return_value = json.dumps(
            valid_spec(open_questions=questions), ensure_ascii=False
        )

        result = self._run()

        self.assertEqual(result.outcome, ProductManagerWorkflowOutcome.AWAITING_APPROVAL)
        self.assertEqual(result.open_questions, questions)
        self._assert_published(result)

    def test_completed_workflow_replays_from_sql_without_calling_model_again(self):
        first = self._run()
        with self.session_factory() as db:
            before = (
                db.scalar(select(func.count()).select_from(Plan)),
                db.scalar(select(func.count()).select_from(Task)),
                db.scalar(select(func.count()).select_from(ConfigurationItem)),
                db.scalar(select(func.count()).select_from(TaskResult)),
            )

        second = self._run()

        self.assertEqual(second, first)
        self.chat.assert_called_once()
        with self.session_factory() as db:
            after = (
                db.scalar(select(func.count()).select_from(Plan)),
                db.scalar(select(func.count()).select_from(Task)),
                db.scalar(select(func.count()).select_from(ConfigurationItem)),
                db.scalar(select(func.count()).select_from(TaskResult)),
            )
        self.assertEqual(after, before)

    def test_resumes_when_plan_exists_but_task_has_not_been_claimed(self):
        plan, task = self._create_plan()
        self.assertEqual((plan.status, task.status), ("pending", "pending"))

        result = self._run()

        self.assertEqual((result.plan_id, result.task_id), (plan.plan_id, task.task_id))
        self._assert_published(result)
        self.chat.assert_called_once()

    def test_resumes_after_claim_and_after_a_model_failure(self):
        plan, task = self._create_plan()
        with self.session_factory() as db:
            task_service.claim_product_manager_task(
                db, self.owner, self.project.id, self.run.run_id, task.task_id
            )
        self.chat.side_effect = [RuntimeError("temporary model failure"), self.chat.return_value]

        with self.assertRaisesRegex(RuntimeError, "temporary model failure"):
            self._run()
        with self.session_factory() as db:
            self.assertEqual(db.get(Plan, plan.id).status, "running")
            self.assertEqual(db.get(Task, task.id).status, "running")
            self.assertEqual(db.scalar(select(func.count()).select_from(ConfigurationItem)), 0)
            execution_id = task_execution.latest_execution(db, task.task_id).execution_id

        with self.assertRaises(ConflictException):
            self._run()
        result = self._run(recovery_execution_id=execution_id)

        self.assertEqual((result.plan_id, result.task_id), (plan.plan_id, task.task_id))
        self.assertEqual(self.chat.call_count, 2)
        self._assert_published(result)

    def test_owner_and_terminal_run_are_rechecked_before_replay(self):
        with self.assertRaises(NotFoundException):
            self._run(user=self.outsider)
        self.chat.assert_not_called()

        completed = self._run()
        with self.session_factory() as db:
            run = db.get(BuildRun, self.run.id)
            run.status = "failed"
            run.active_slot = None
            db.commit()

        with self.assertRaises(ConflictException):
            self._run()
        self.assertEqual(self.chat.call_count, 1)
        self._assert_published(completed, run_state=("failed", "pm", None))

    def test_graph_contains_explicit_resume_and_question_routes(self):
        graph = product_manager_workflow.build_product_manager_workflow(self.session_factory)
        drawable = graph.get_graph()

        self.assertEqual(
            {
                "ensure_plan",
                "claim_task",
                "start_execution",
                "generate_app_spec",
                "complete_app_spec",
                "load_saved_app_spec",
                "needs_user_input",
                "awaiting_approval",
                "ready_for_design",
                "assign_design_task",
            },
            {name for name in drawable.nodes if not name.startswith("__")},
        )
        edges = {(edge.source, edge.target, edge.conditional) for edge in drawable.edges}
        self.assertIn(("ensure_plan", "claim_task", True), edges)
        self.assertIn(("ensure_plan", "load_saved_app_spec", True), edges)
        self.assertIn(("load_saved_app_spec", "needs_user_input", True), edges)
        self.assertIn(("load_saved_app_spec", "awaiting_approval", True), edges)
        self.assertIn(("load_saved_app_spec", "ready_for_design", True), edges)
        self.assertIn(("ready_for_design", "assign_design_task", False), edges)


if __name__ == "__main__":
    unittest.main()
