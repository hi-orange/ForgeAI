"""Approval compatibility and provenance regressions using isolated SQLite databases."""

import hashlib
import json
from unittest.mock import patch

from sqlalchemy import func, null, select, update
from test_product_manager_workflow import (
    ProductManagerWorkflowFixture,
    approval_payload,
    valid_spec,
)

from app.core.exceptions import ConflictException
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.project import Project
from app.models.task import Task
from app.models.task_artifact import TaskArtifact
from app.models.task_execution import TaskExecution
from app.schemas.app_spec import AppSpec
from app.schemas.plan import PlanCreate
from app.schemas.project_message import ProjectMessageCreate
from app.schemas.requirements import RequirementsApproval
from app.schemas.task import TaskCreate
from app.services import plan as plan_service
from app.services import product_manager, project_manager
from app.services.requirement_approval import approve_requirements
from app.services.requirement_inputs import read_app_spec
from app.services.requirements import get_requirements_status
from app.services.task_artifact import TaskArtifactRole, stage_task_side_artifact


def legacy_spec():
    payload = valid_spec()
    for field in (
        "features",
        "data_requirements",
        "interface_requirements",
        "constraints",
        "acceptance_criteria",
    ):
        payload[field] = [item["text"] for item in payload[field]]
    return payload


class ApprovalContractTests(ProductManagerWorkflowFixture):
    def _status(self):
        with self.session_factory() as db:
            return get_requirements_status(db, self.owner, self.project.id)

    def _legacy_result(self):
        result = self._run()
        payload = legacy_spec()
        # Simulate rows produced by the previous release, without using today's writer.
        with self.session_factory() as db:
            db.execute(
                update(ConfigurationItem)
                .where(ConfigurationItem.item_id == result.configuration_item_id)
                .values(
                    schema_version=1,
                    payload=payload,
                    content_hash=hashlib.sha256(json.dumps(payload).encode()).hexdigest(),
                )
            )
            db.commit()
        return result, payload

    def _approve(self, result, payload):
        with self.session_factory() as db:
            return approve_requirements(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                result.configuration_item_id,
                RequirementsApproval.model_validate(payload),
            )

    def test_v1_refresh_replay_and_approval_preserve_original(self):
        result, original = self._legacy_result()
        status = self._status()
        self.assertEqual(status.state, "awaiting_approval")
        self.assertEqual(status.app_spec, self._status().app_spec)
        self.assertEqual(self._run().configuration_item_id, result.configuration_item_id)
        self.chat.assert_called_once()
        payload = approval_payload(status.app_spec.model_dump())
        approved_id = self._approve(result, payload)
        self.assertEqual(self._approve(result, payload), approved_id)
        with self.session_factory() as db:
            old = db.scalar(
                select(ConfigurationItem).where(
                    ConfigurationItem.item_id == result.configuration_item_id
                )
            )
            new = db.scalar(
                select(ConfigurationItem).where(ConfigurationItem.item_id == approved_id)
            )
            self.assertEqual((old.schema_version, old.payload), (1, original))
            self.assertEqual(new.schema_version, 2)
            self.assertEqual(new.upstream_item_ids, [old.item_id])
            delivery = project_manager.create_engineering_delivery_task(
                db, self.owner, self.project.id, self.run.run_id, approved_id
            )
            self.assertEqual(delivery.input_configuration_item_ids, [approved_id])

    def test_v1_clarification_uses_normalized_pinned_input(self):
        result, original = self._legacy_result()
        with self.session_factory() as db:
            plan = project_manager.create_clarification_plan(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                result.configuration_item_id,
                ProjectMessageCreate(content="支持 CSV 导出", client_message_id="legacy-answer"),
            )
            from app.orchestration.product_manager import run_product_manager_workflow

            run_product_manager_workflow(
                db, self.owner, self.project.id, self.run.run_id, plan.cause_message_id
            )
        sent = json.loads(self.chat.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(sent["previous_item_id"], result.configuration_item_id)
        self.assertEqual(sent["previous_app_spec"], AppSpec.model_validate(original).model_dump())

    def test_real_legacy_pending_task_waits_for_approval_then_is_replaced_once(self):
        result, original = self._legacy_result()
        with self.session_factory() as db:
            legacy = plan_service.save_plan(
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
                            recipient="SolutionArchitect",
                            title="旧设计",
                            instructions="legacy",
                            expected_output_type="system_design",
                            input_configuration_item_ids=[result.configuration_item_id],
                        )
                    ],
                ),
            )
        status = self._status()
        self.assertEqual(status.state, "awaiting_approval")
        self.assertIsNone(status.result.design_task_id)
        self.assertEqual(self._run().outcome, "awaiting_approval")
        # Historical automatic dispatch is not an approval signal.
        with self.session_factory() as db, self.assertRaises(ConflictException):
            project_manager.create_engineering_delivery_task(
                db, self.owner, self.project.id, self.run.run_id, result.configuration_item_id
            )
        payload = approval_payload(status.app_spec.model_dump())
        invalid = approval_payload(status.app_spec.model_dump())
        invalid["selected"][0]["text"] = "修改过的功能"
        with self.assertRaises(ConflictException):
            self._approve(result, invalid)
        with self.session_factory() as db:
            self.assertEqual(
                db.scalar(select(Plan.status).where(Plan.plan_id == legacy.plan_id)), "pending"
            )
        approved_id = self._approve(result, payload)
        self.assertEqual(self._approve(result, payload), approved_id)
        with self.session_factory() as db:
            old_plan = db.scalar(select(Plan).where(Plan.plan_id == legacy.plan_id))
            old_task = db.scalar(select(Task).where(Task.plan_id == legacy.plan_id))
            self.assertEqual((old_plan.status, old_task.status), ("cancelled", "cancelled"))
            delivery = project_manager.create_engineering_delivery_task(
                db, self.owner, self.project.id, self.run.run_id, approved_id
            )
            replay = project_manager.create_engineering_delivery_task(
                db, self.owner, self.project.id, self.run.run_id, approved_id
            )
            self.assertEqual(delivery.task_id, replay.task_id)
            old = db.scalar(
                select(ConfigurationItem).where(
                    ConfigurationItem.item_id == result.configuration_item_id
                )
            )
            self.assertEqual(old.payload, original)

    def test_legacy_pending_assignment_can_be_replaced_by_clarification(self):
        result, _ = self._legacy_result()
        with self.session_factory() as db:
            legacy = plan_service.save_plan(
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
                            recipient="SolutionArchitect",
                            title="旧设计",
                            instructions="legacy",
                            expected_output_type="system_design",
                            input_configuration_item_ids=[result.configuration_item_id],
                        )
                    ],
                ),
            )
            answer = ProjectMessageCreate(
                content="CSV 导出", client_message_id="legacy-pending-answer"
            )
            followup = project_manager.create_clarification_plan(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                result.configuration_item_id,
                answer,
            )
            replay = project_manager.create_clarification_plan(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                result.configuration_item_id,
                answer,
            )
            self.assertEqual(followup.version, 3)
            self.assertEqual(followup.plan_id, replay.plan_id)
            self.assertEqual(
                db.scalar(select(Plan.status).where(Plan.plan_id == legacy.plan_id)), "cancelled"
            )
            from app.orchestration.product_manager import run_product_manager_workflow

            completed = run_product_manager_workflow(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                followup.cause_message_id,
            )
            self.assertEqual(completed.outcome, "awaiting_approval")

    def test_unknown_version_rejected_by_read_approval_clarification_and_replay(self):
        result = self._run()
        with self.session_factory() as db:
            db.execute(update(ConfigurationItem).values(schema_version=99))
            db.commit()
        for action in (self._status, self._run, lambda: self._approve(result, approval_payload())):
            with self.assertRaises(ConflictException):
                action()
        with self.session_factory() as db, self.assertRaises(ConflictException):
            project_manager.create_clarification_plan(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                result.configuration_item_id,
                ProjectMessageCreate(content="CSV", client_message_id="unsupported"),
            )

    def test_v1_saved_draft_recovers_without_another_model_call(self):
        with patch.object(product_manager, "complete_task_app_spec", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                self._run()
        status = self._status()
        with self.session_factory() as db:
            execution = db.get(TaskExecution, status.execution_id)
            draft = dict(execution.draft)
            draft.update(schema_version=1, app_spec=legacy_spec())
            execution.draft = draft
            db.commit()
        result = self._run(recovery_execution_id=status.execution_id)
        self.chat.assert_called_once()
        with self.session_factory() as db:
            item = db.scalar(
                select(ConfigurationItem).where(
                    ConfigurationItem.item_id == result.configuration_item_id
                )
            )
            self.assertEqual(item.schema_version, 2)
            self.assertEqual(read_app_spec(item).goal, valid_spec()["goal"])

    def test_edited_feature_requires_explicit_acceptance_and_keeps_unchanged_criteria(self):
        result = self._run()
        payload = approval_payload()
        payload["selected"][0]["text"] = "仅浏览预置记录，不允许添加记录"
        with self.assertRaises(ConflictException):
            self._approve(result, payload)
        self.assertEqual(self._status().state, "awaiting_approval")
        payload["selected"][0]["acceptance"] = "打开列表显示预置记录，页面没有新增入口"
        approved_id = self._approve(result, payload)
        with self.session_factory() as db:
            item = db.scalar(
                select(ConfigurationItem).where(ConfigurationItem.item_id == approved_id)
            )
            criteria = item.payload["acceptance_criteria"]
            self.assertNotIn("ac_add", [criterion["id"] for criterion in criteria])
            self.assertIn(valid_spec()["acceptance_criteria"][1], criteria)
            edited = [c for c in criteria if c["source_ids"] == ["feat_records"]]
            self.assertEqual([c["text"] for c in edited], [payload["selected"][0]["acceptance"]])

    def test_new_feature_cannot_get_a_fabricated_acceptance(self):
        result = self._run()
        payload = approval_payload()
        payload["selected"].append({"id": "feat_search", "kind": "feature", "text": "按书名搜索"})
        with self.assertRaises(ConflictException):
            self._approve(result, payload)
        payload["selected"][-1]["acceptance"] = "输入完整书名后只展示该书，无匹配时显示空结果"
        self._approve(result, payload)

    def test_removing_part_of_joint_acceptance_does_not_reintroduce_deleted_scope(self):
        spec = valid_spec()
        spec["acceptance_criteria"] = [
            {
                "id": "ac_joint",
                "text": "新增后导出并下载记录",
                "source_ids": ["feat_records", "feat_export"],
            }
        ]
        self.chat.return_value = json.dumps(spec)
        result = self._run()
        payload = approval_payload(spec)
        payload["selected"] = [s for s in payload["selected"] if s["id"] != "feat_export"]
        with self.assertRaises(ConflictException):
            self._approve(result, payload)
        payload["selected"][0]["acceptance"] = "新增后在列表中显示记录"
        approved = self._approve(result, payload)
        with self.session_factory() as db:
            item = db.scalar(select(ConfigurationItem).where(ConfigurationItem.item_id == approved))
            self.assertNotIn("ac_joint", [c["id"] for c in item.payload["acceptance_criteria"]])

    def test_side_artifact_rejects_cross_project_and_cross_run_without_writing_link(self):
        _, task = self._create_plan()
        with self.session_factory() as db:
            other_project = Project(user_id=self.outsider.id, name="Other", prompt="Other")
            db.add(other_project)
            db.flush()
            for project_id, run_id in (
                (other_project.id, "run_other_project"),
                (self.project.id, "run_previous"),
            ):
                with self.subTest(run_id=run_id):
                    db.add(
                        BuildRun(
                            project_id=project_id,
                            run_id=run_id,
                            status="succeeded",
                            active_slot=null(),
                        )
                    )
                    db.flush()
                    item = ConfigurationItem(
                        item_id=f"ci_{run_id}",
                        project_id=project_id,
                        producer_run_id=run_id,
                        semantic_type="system_design",
                        version=1,
                        payload={"summary": "old"},
                        content_hash="a" * 64,
                        upstream_item_ids=[],
                        state="usable",
                    )
                    db.add(item)
                    db.flush()
                    with self.assertRaises(ConflictException):
                        stage_task_side_artifact(
                            db,
                            task_id=task.task_id,
                            configuration_item_id=item.item_id,
                            artifact_role=TaskArtifactRole.SYSTEM_DESIGN,
                        )
                    self.assertEqual(db.scalar(select(func.count()).select_from(TaskArtifact)), 0)

    def test_another_tasks_primary_result_cannot_be_claimed_as_side_output(self):
        result = self._run()
        approved_id = self._approve(result, approval_payload())
        with self.session_factory() as db:
            task = project_manager.create_engineering_delivery_task(
                db, self.owner, self.project.id, self.run.run_id, approved_id
            )
            with self.assertRaises(ConflictException):
                stage_task_side_artifact(
                    db,
                    task_id=task.task_id,
                    configuration_item_id=approved_id,
                    artifact_role=TaskArtifactRole.OTHER,
                )
            self.assertEqual(db.scalar(select(func.count()).select_from(TaskArtifact)), 0)
