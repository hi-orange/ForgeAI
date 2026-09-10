import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy import event, func, select, update
from test_product_manager_workflow import (
    ProductManagerWorkflowFixture,
    approval_payload,
    valid_spec,
)

from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.project_message import ProjectMessage
from app.models.task import Task, TaskRecipient
from app.models.task_execution import TaskExecution
from app.models.task_result import TaskResult
from app.schemas.plan import PlanCreate
from app.schemas.product_manager_workflow import ProductManagerWorkflowResult
from app.schemas.requirements import RequirementsApproval
from app.schemas.task import TaskCreate
from app.services import plan as plan_service
from app.services import product_manager, project_manager
from app.services import task as task_service
from app.services.design_handoff import APPROVAL_VERSION
from app.services.requirement_approval import approve_requirements
from app.services.requirements import get_requirements_status
from app.services.task_execution import utc_now


class DesignHandoffTests(ProductManagerWorkflowFixture):
    def test_response_requires_paired_design_identifiers_and_resolved_questions(self):
        result = self._run().model_dump()
        result.update(
            {
                "outcome": "ready_for_design",
                "design_plan_id": "plan-design",
                "design_task_id": "task-design",
                "open_questions": [],
            }
        )
        for overrides in (
            {"design_task_id": None},
            {"design_plan_id": None},
            {"open_questions": ["还没回答"]},
            {"outcome": "needs_user_input"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                ProductManagerWorkflowResult.model_validate({**result, **overrides})

    def test_unfinished_source_task_is_not_accepted_even_if_a_result_exists(self):
        item = self._publish_only()
        with self.session_factory() as db:
            source = db.scalar(select(Task))
            source.status = "running"
            db.commit()
        with self.assertRaises(ConflictException):
            self._assign(item.item_id)
        self.assertEqual(self._count(Plan), 1)

    def _publish_only(self, questions=None):
        _, task = self._create_plan()
        self.chat.return_value = json.dumps(valid_spec(open_questions=questions))
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
            # Direct handoff tests model the post-approval state. The question case
            # intentionally keeps the original result so it cannot be dispatched.
            if questions is None:
                db.execute(
                    update(TaskResult)
                    .where(TaskResult.task_id == task.task_id)
                    .values(prompt_version=APPROVAL_VERSION)
                )
                db.commit()
            return result

    def _assign(self, item_id, user=None, run_id=None):
        with self.session_factory() as db:
            # Manual handoff tests now pass through the same approval boundary as
            # the HTTP endpoint. Question-bearing specs remain unapproved so the
            # rejection tests continue to exercise that guard.
            item = db.get(ConfigurationItem, item_id)
            if item is not None:
                spec = item.payload
                result = db.scalar(
                    select(TaskResult).where(TaskResult.configuration_item_id == item_id)
                )
                if not spec.get("open_questions") and (
                    result is None or result.prompt_version != APPROVAL_VERSION
                ):
                    approved_id = approve_requirements(
                        db,
                        user or self.owner,
                        self.project.id,
                        run_id or self.run.run_id,
                        item_id,
                        RequirementsApproval.model_validate(
                            approval_payload(
                                spec,
                                client_message_id=f"test-approval-{item_id}",
                            )
                        ),
                    )
                    item_id = approved_id
            return project_manager.create_engineering_delivery_task(
                db,
                user or self.owner,
                self.project.id,
                run_id or self.run.run_id,
                item_id,
            )

    def _status(self):
        with self.session_factory() as db:
            return get_requirements_status(db, self.owner, self.project.id)

    def _count(self, model):
        with self.session_factory() as db:
            return db.scalar(select(func.count()).select_from(model))

    def test_workflow_assigns_one_pending_design_task_without_executing_it(self):
        result = self._publish_only()
        assigned = self._assign(result.item_id)
        with self.session_factory() as db:
            plan = db.scalar(select(Plan).where(Plan.plan_id == assigned.plan_id))
            design_task = db.scalar(select(Task).where(Task.task_id == assigned.task_id))
            self.assertEqual(plan.version, 2)
            self.assertEqual(plan.cause_message_id, self.message.id)
            self.assertEqual((plan.status, design_task.status), ("pending", "pending"))
            self.assertEqual(design_task.plan_id, plan.plan_id)
            self.assertEqual(design_task.recipient, "SoftwareEngineer")
            self.assertEqual(design_task.expected_output_type, "code")
            self.assertEqual(design_task.task_key, "engineering_delivery")
            self.assertEqual(design_task.input_configuration_item_ids, [result.item_id])
            self.assertEqual(design_task.depends_on_task_ids, [])
            run = db.get(BuildRun, self.run.id)
            self.assertEqual((run.status, run.stage, run.active_slot), ("running", "pm", 1))
            with self.assertRaises(BusinessException):
                task_service.claim_product_manager_task(
                    db,
                    self.owner,
                    self.project.id,
                    self.run.run_id,
                    design_task.task_id,
                )
        self.chat.assert_called_once()
        self.assertEqual(self._count(ConfigurationItem), 1)
        self.assertEqual(self._count(TaskExecution), 0)
        self.assertEqual(self._count(TaskResult), 1)
        progress = self._status()
        self.assertEqual(progress.state, "design_pending")
        self.assertEqual((progress.plan_id, progress.task_id), (assigned.plan_id, assigned.task_id))
        self.assertEqual(progress.result.configuration_item_id, result.item_id)
        self.assertEqual(progress.app_spec.model_dump(), valid_spec())

    def test_questions_never_create_a_design_plan(self):
        self.chat.return_value = json.dumps(valid_spec(open_questions=["导出格式？"]))
        result = self._run()
        self.assertIsNone(result.design_task_id)
        self.assertIsNone(result.design_plan_id)
        with self.assertRaisesRegex(ConflictException, "待确认问题"):
            self._assign(result.configuration_item_id)
        self.assertEqual(self._count(Plan), 1)
        self.assertEqual(self._count(Task), 1)
        self.assertEqual(self._status().state, "awaiting_approval")

    def test_concurrent_handoffs_create_one_task_and_replay_without_model_calls(self):
        item = self._publish_only()
        barrier = Barrier(2)

        def assign():
            barrier.wait(timeout=10)
            return self._assign(item.item_id).task_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(assign) for _ in range(2)]
            ids = [future.result(timeout=20) for future in futures]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self._assign(item.item_id).task_id, ids[0])
        result = self._run()
        self.assertEqual(result.design_task_id, ids[0])
        self.assertEqual(self._count(Plan), 2)
        self.assertEqual(self._count(Task), 2)
        self.chat.assert_called_once()

    def test_newer_message_does_not_replace_the_pinned_requirement(self):
        item = self._publish_only()
        with self.session_factory() as db:
            db.add(
                ProjectMessage(
                    project_id=self.project.id,
                    sequence=3,
                    sender="user",
                    content="后来又提出的需求",
                    client_message_id="newer-than-published",
                )
            )
            db.commit()
        assigned = self._assign(item.item_id)
        with self.session_factory() as db:
            plan = db.scalar(select(Plan).where(Plan.plan_id == assigned.plan_id))
            self.assertEqual(plan.cause_message_id, self.message.id)
        self.assertEqual(assigned.input_configuration_item_ids, [item.item_id])

    def test_owner_run_and_unpublished_inputs_are_rejected(self):
        item = self._publish_only()
        with self.assertRaises(NotFoundException):
            self._assign(item.item_id, user=self.outsider)
        with self.assertRaises(NotFoundException):
            self._assign(item.item_id, run_id="missing-run")
        with self.assertRaises(NotFoundException):
            self._assign("missing-item")
        with self.session_factory() as db:
            saved = db.scalar(
                select(TaskResult).where(TaskResult.configuration_item_id == item.item_id)
            )
            db.delete(saved)
            db.commit()
        with self.assertRaises(NotFoundException):
            self._assign(item.item_id)
        self.assertEqual(self._count(Plan), 1)

    def test_invalidated_requirement_cannot_be_assigned_or_replayed(self):
        item = self._publish_only()
        self._assign(item.item_id)
        with self.session_factory() as db:
            original = db.get(ConfigurationItem, item.id)
            original.state = "unusable"
            original.unusable_reason = "test invalidation"
            original.unusable_at = utc_now()
            db.commit()
        with self.assertRaises(ConflictException):
            self._assign(item.item_id)
        with self.assertRaises(ConflictException):
            self._status()
        self.assertEqual(self._count(Plan), 2)

    def test_terminal_run_rejects_handoff(self):
        item = self._publish_only()
        with self.session_factory() as db:
            run = db.get(BuildRun, self.run.id)
            run.status, run.active_slot = "failed", None
            db.commit()
        with self.assertRaises(ConflictException):
            self._assign(item.item_id)
        self.assertEqual(self._count(Plan), 1)

    def test_cancelled_design_task_is_not_resurrected(self):
        item = self._publish_only()
        assigned = self._assign(item.item_id)
        with self.session_factory() as db:
            db.get(Task, assigned.id).status = "cancelled"
            db.commit()
        with self.assertRaises(ConflictException):
            self._assign(item.item_id)
        self.assertEqual(self._status().state, "stopped")
        self.assertEqual(self._count(Task), 2)

    def test_competing_later_plan_is_not_overwritten_or_treated_as_design(self):
        item = self._publish_only()
        with self.session_factory() as db:
            plan_service.save_plan(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                PlanCreate(
                    version=2,
                    cause_message_id=self.message.id,
                    tasks=[
                        TaskCreate(
                            task_key="other",
                            recipient=TaskRecipient.PRODUCT_MANAGER,
                            title="其他任务",
                            instructions="另外安排的需求任务",
                            expected_output_type="app_spec",
                        )
                    ],
                ),
            )
        with self.assertRaises(ConflictException):
            self._assign(item.item_id)
        with self.assertRaises(ConflictException):
            self._run()
        self.assertEqual(self._count(Plan), 2)
        self.chat.assert_called_once()

    def test_more_recent_plan_prevents_replaying_old_handoff(self):
        item = self._publish_only()
        self._assign(item.item_id)
        with self.session_factory() as db:
            plan_service.save_plan(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                PlanCreate(
                    version=3,
                    cause_message_id=self.later_message.id,
                    tasks=[
                        TaskCreate(
                            task_key="other",
                            recipient=TaskRecipient.PRODUCT_MANAGER,
                            title="后续任务",
                            instructions="处理后续消息",
                            expected_output_type="app_spec",
                        )
                    ],
                ),
            )
        with self.assertRaises(ConflictException):
            self._assign(item.item_id)
        with self.assertRaises(ConflictException):
            self._run()
        self.assertEqual(self._count(Plan), 3)

    def test_plan_and_task_creation_roll_back_together(self):
        item = self._publish_only()

        def fail(*_):
            raise RuntimeError("task insert failed")

        event.listen(Task, "before_insert", fail)
        try:
            with self.assertRaisesRegex(RuntimeError, "task insert failed"):
                self._assign(item.item_id)
        finally:
            event.remove(Task, "before_insert", fail)
        self.assertEqual(self._count(Plan), 1)
        self.assertEqual(self._count(Task), 1)
        self.assertEqual(self._count(ConfigurationItem), 1)
        self.assertEqual(self._status().state, "ready_for_design")
        self._assign(item.item_id)

    def test_handoff_failure_retries_only_dispatch_not_product_manager(self):
        item = self._publish_only()
        with patch.object(
            project_manager,
            "create_engineering_delivery_task",
            side_effect=RuntimeError("dispatch failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "dispatch failed"):
                self._assign(item.item_id)
        progress = self._status()
        self.assertEqual(progress.state, "ready_for_design")
        self.assertEqual(self._count(TaskResult), 1)
        result = self._assign(item.item_id)
        self.assertEqual(result.input_configuration_item_ids, [item.item_id])
        self.assertEqual(self._status().state, "design_pending")
        self.chat.assert_called_once()

    def test_reading_legacy_ready_requirements_does_not_dispatch(self):
        self._publish_only()
        for _ in range(2):
            self.assertEqual(self._status().state, "ready_for_design")
        self.assertEqual(self._count(Plan), 1)
        self.chat.assert_called_once()

    def test_corrupt_or_unsupported_source_is_rejected(self):
        item = self._publish_only()
        for values in (
            {"schema_version": 99},
            {"schema_version": 1, "payload": {"goal": "invalid"}},
        ):
            with self.subTest(values=values), self.session_factory() as db:
                # 模拟历史坏数据，绕开生产 ORM 不可变保护，仅修改隔离夹具。
                db.execute(
                    update(ConfigurationItem)
                    .where(ConfigurationItem.item_id == item.item_id)
                    .values(**values)
                )
                db.commit()
                with self.assertRaises(ConflictException):
                    self._assign(item.item_id)
        self.assertEqual(self._count(Plan), 1)

    def test_approval_keeps_selected_ids_and_drops_deselected_features(self):
        _, task = self._create_plan()
        self.chat.return_value = json.dumps(valid_spec())
        with self.session_factory() as db:
            task_service.claim_product_manager_task(
                db, self.owner, self.project.id, self.run.run_id, task.task_id
            )
            draft = product_manager.generate_task_app_spec(
                db, self.owner, self.project.id, self.run.run_id, task.task_id
            )
            proposal = product_manager.complete_task_app_spec(
                db, self.owner, self.project.id, self.run.run_id, task.task_id, draft
            )
        payload = approval_payload(
            client_message_id="deselect-approval",
            selected=[
                {"id": "feat_records", "text": "新增和查看读书记录", "kind": "feature"},
                {"id": "data_title", "text": "书名", "kind": "data"},
                {"id": "con_private", "text": "数据仅本人可见", "kind": "constraint"},
            ],
        )
        with self.session_factory() as db:
            approved_id = approve_requirements(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                proposal.item_id,
                RequirementsApproval.model_validate(payload),
            )
            item = db.scalar(
                select(ConfigurationItem).where(ConfigurationItem.item_id == approved_id)
            )
            assert item is not None
            spec = item.payload
            self.assertEqual([entry["id"] for entry in spec["features"]], ["feat_records"])
            self.assertEqual([entry["id"] for entry in spec["data_requirements"]], ["data_title"])
            self.assertEqual(spec["interface_requirements"], [])
            self.assertNotIn("feat_export", [entry["id"] for entry in spec["features"]])
            self.assertTrue(
                all(
                    set(entry["source_ids"]) <= {"feat_records"}
                    for entry in spec["acceptance_criteria"]
                )
            )
            delivery = project_manager.create_engineering_delivery_task(
                db, self.owner, self.project.id, self.run.run_id, approved_id
            )
            self.assertEqual(delivery.recipient, "SoftwareEngineer")
            self.assertEqual(delivery.expected_output_type, "code")
            self.assertEqual(delivery.input_configuration_item_ids, [approved_id])

    def test_legacy_solution_architect_pending_converts_to_engineering_delivery(self):
        item = self._publish_only()
        with self.session_factory() as db:
            plan_service.save_plan(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                PlanCreate(
                    version=2,
                    cause_message_id=self.message.id,
                    tasks=[
                        TaskCreate(
                            task_key="system_design",
                            recipient=TaskRecipient.SOLUTION_ARCHITECT,
                            title="旧设计任务",
                            instructions="legacy",
                            expected_output_type="system_design",
                            input_configuration_item_ids=[item.item_id],
                        )
                    ],
                ),
            )
        converted = self._assign(item.item_id)
        with self.session_factory() as db:
            legacy = db.scalar(
                select(Task).where(
                    Task.task_key == "system_design",
                    Task.recipient == "SolutionArchitect",
                )
            )
            engineering = db.get(Task, converted.id)
            self.assertEqual(legacy.status, "cancelled")
            self.assertEqual(engineering.recipient, "SoftwareEngineer")
            self.assertEqual(engineering.expected_output_type, "code")
            self.assertEqual(engineering.input_configuration_item_ids, [item.item_id])
            replay = project_manager.create_engineering_delivery_task(
                db, self.owner, self.project.id, self.run.run_id, item.item_id
            )
            self.assertEqual(replay.task_id, engineering.task_id)
        self.assertEqual(self._status().state, "design_pending")

    def test_side_artifacts_can_register_beside_main_result(self):
        from app.models.configuration_item import ConfigurationItemType
        from app.schemas.configuration_item import ConfigurationItemRegistration
        from app.services import configuration_manager, task_artifact
        from app.services.task_artifact import TaskArtifactRole

        item = self._publish_only()
        assigned = self._assign(item.item_id)
        with self.session_factory() as db:
            design = configuration_manager.stage_configuration_item(
                db,
                project_id=self.project.id,
                producer_run_id=self.run.run_id,
                submission=ConfigurationItemRegistration(
                    semantic_type=ConfigurationItemType.SYSTEM_DESIGN,
                    schema_version=1,
                    payload={"summary": "brief"},
                    upstream_item_ids=[item.item_id],
                ),
            )
            code = configuration_manager.stage_configuration_item(
                db,
                project_id=self.project.id,
                producer_run_id=self.run.run_id,
                submission=ConfigurationItemRegistration(
                    semantic_type=ConfigurationItemType.CODE,
                    schema_version=1,
                    payload={"revision": "rev_test"},
                    upstream_item_ids=[item.item_id],
                ),
            )
            db.add(
                TaskResult(
                    task_id=assigned.task_id,
                    configuration_item_id=code.item_id,
                    result_hash="a" * 64,
                    source_message_ids=[self.message.id],
                    context_truncated=False,
                    model="test",
                    prompt_version="test",
                )
            )
            side = task_artifact.stage_task_side_artifact(
                db,
                task_id=assigned.task_id,
                configuration_item_id=design.item_id,
                artifact_role=TaskArtifactRole.SYSTEM_DESIGN,
            )
            with self.assertRaises(ConflictException):
                task_artifact.stage_task_side_artifact(
                    db,
                    task_id=assigned.task_id,
                    configuration_item_id=code.item_id,
                    artifact_role=TaskArtifactRole.OTHER,
                )
            db.commit()
            listed = task_artifact.list_task_side_artifacts(db, assigned.task_id)
            self.assertEqual([entry.id for entry in listed], [side.id])
            self.assertEqual(listed[0].configuration_item_id, design.item_id)
