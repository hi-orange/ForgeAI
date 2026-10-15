"""Real tool-loop recovery with mocked providers and isolated persistent databases."""

import importlib.util
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, inspect, select
from test_product_manager_workflow import ProductManagerWorkflowFixture, approval_payload

from app.agents import leader as leader_agent
from app.agents.leader import lead_project_turn
from app.core.exceptions import BusinessException, ConflictException, ProviderException
from app.models.agent_decision_event import AgentDecisionEvent
from app.models.build_run import BuildRun
from app.models.leader_dispatch_checkpoint import LeaderDispatchCheckpoint
from app.models.plan import Plan
from app.models.project import Project
from app.schemas.agent_action import ChatWithToolsResult, ToolCall
from app.schemas.requirements import RequirementsApproval
from app.services import leader
from app.services.app_spec import approve_requirements
from app.services.engineering.handoff import load_approved_app_spec
from app.services.leader_dispatch import claim_dispatch, release_dispatch, save_dispatch
from app.services.task_execution import utc_now
from app.services.workflow_jobs import pause_jobs_for_run


def turn(name, arguments=None):
    return ChatWithToolsResult(tool_calls=[ToolCall(id=name, name=name, arguments=arguments or {})])


class LeaderDispatchRecoveryTests(ProductManagerWorkflowFixture):
    def setUp(self):
        super().setUp()
        result = self._run()
        with self.session_factory() as db:
            self.approved_id = approve_requirements(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                result.configuration_item_id,
                RequirementsApproval.model_validate(approval_payload()),
            )
            _, _, source_plan, _ = load_approved_app_spec(
                db, self.project.id, self.run.run_id, self.approved_id
            )
            self.context = leader.build_leader_context(
                db,
                project_id=self.project.id,
                run_id=self.run.run_id,
                target_message_id=source_plan.cause_message_id,
            )
        self.leader.side_effect = lead_project_turn

    def _dispatch(self):
        with self.session_factory() as db:
            return leader.dispatch_approved_requirements(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                self.approved_id,
            )

    def _turns(self):
        return [
            turn("read_project_context"),
            turn(
                "classify_intent",
                {
                    "intent": {
                        "category": "product_change",
                        "priority": "normal",
                        "summary": "小范围需求",
                    }
                },
            ),
            turn(
                "create_plan",
                {
                    "plan": {
                        "version": max(p.version for p in self.context.plans) + 1,
                        "cause_message_id": self.context.target_message.id,
                        "tasks": [
                            {
                                "task_key": "engineering_delivery",
                                "recipient": "Code Engineer",
                                "title": "交付",
                                "instructions": "small_extension: 完成已批准需求",
                                "expected_output_type": "code",
                                "input_configuration_item_ids": [self.approved_id],
                            }
                        ],
                    }
                },
            ),
            turn("dispatch_task", {"task_key": "engineering_delivery"}),
            turn("finish_turn", {"action": "dispatch", "summary": "交付已批准需求"}),
        ]

    def test_provider_failure_restores_intent_plan_dispatch_and_history(self):
        with patch.object(
            leader_agent,
            "chat_with_tools",
            side_effect=[
                *self._turns()[:-1],
                ProviderException("busy", retryable=True),
            ],
        ):
            with self.assertRaises(ProviderException):
                self._dispatch()
        with self.session_factory() as db:
            saved = db.get(LeaderDispatchCheckpoint, self.approved_id)
            self.assertEqual(saved.tool_loop["turns"], 5)
            self.assertEqual(
                saved.tool_loop["state"]["dispatched_task_keys"], ["engineering_delivery"]
            )
            self.assertIsNone(saved.lease_owner)
            frozen = deepcopy(saved.frozen_input)
            history = deepcopy(saved.tool_loop["history"])
            db.get(Project, self.project.id).name = "Changed after frozen input"
            db.commit()
        with patch.object(leader_agent, "chat_with_tools", return_value=self._turns()[-1]) as chat:
            task = self._dispatch()
            chat.assert_called_once()
            self.assertEqual(chat.call_args.kwargs["messages"], history)
            replay = self._dispatch()
            self.assertEqual(replay.task_id, task.task_id)
            chat.assert_called_once()
        with self.session_factory() as db:
            saved = db.get(LeaderDispatchCheckpoint, self.approved_id)
            self.assertEqual(saved.frozen_input, frozen)
            self.assertEqual(saved.tool_loop["turns"], 6)
            self.assertEqual(db.scalar(select(func.count()).select_from(Plan)), 3)
            events = list(db.scalars(select(AgentDecisionEvent)))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].status, "applied")

    def test_migration_upgrade_and_downgrade_preserve_existing_runs(self):
        migration_path = (
            Path(__file__).parents[1]
            / "alembic/versions/a4b6c8d0e2f4_leader_dispatch_checkpoint.py"
        )
        spec = importlib.util.spec_from_file_location("dispatch_migration", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        LeaderDispatchCheckpoint.__table__.drop(self.engine)
        with self.engine.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            schema = inspect(connection)
            self.assertIn("leader_dispatch_checkpoint", schema.get_table_names())
            self.assertEqual(len(schema.get_foreign_keys("leader_dispatch_checkpoint")), 2)
            migration.downgrade()
            self.assertNotIn("leader_dispatch_checkpoint", inspect(connection).get_table_names())
            migration.upgrade()
        with self.session_factory() as db:
            self.assertEqual(db.get(BuildRun, self.run.id).status, "running")

    def test_invalid_dispatch_result_can_be_corrected_without_resetting_history(self):
        with patch.object(
            leader_agent,
            "chat_with_tools",
            side_effect=[
                self._turns()[1],
                turn("finish_turn", {"action": "finish", "summary": "没有分派"}),
            ],
        ):
            with self.assertRaisesRegex(BusinessException, "选择下一个交付角色"):
                self._dispatch()
        with self.session_factory() as db:
            saved = db.get(LeaderDispatchCheckpoint, self.approved_id)
            self.assertNotIn("output", saved.tool_loop)
            self.assertEqual(saved.tool_loop["turns"], 2)
            self.assertIn("分派结果未被应用", saved.tool_loop["history"][-1]["content"])
        with patch.object(leader_agent, "chat_with_tools", side_effect=self._turns()[2:]) as chat:
            self._dispatch()
            self.assertEqual(chat.call_count, 3)

    def test_completed_decision_survives_failed_handoff(self):
        with patch.object(leader_agent, "chat_with_tools", side_effect=self._turns()) as chat:
            with patch.object(
                leader, "create_engineering_delivery_task", side_effect=RuntimeError("disk")
            ):
                with self.assertRaisesRegex(RuntimeError, "disk"):
                    self._dispatch()
            self.assertEqual(chat.call_count, 5)
            with self.session_factory() as db:
                event = db.scalar(select(AgentDecisionEvent))
                self.assertEqual(event.status, "failed")
            self._dispatch()
            self.assertEqual(chat.call_count, 5)

    def test_started_role_does_not_show_stale_dispatch_error(self):
        from app.services.engineering import claim_code_engineer_task
        from app.services.requirements import DISPATCH_ERROR_PREFIX, get_requirements_status

        with patch.object(leader_agent, "chat_with_tools", side_effect=self._turns()):
            task = self._dispatch()
        with self.session_factory() as db:
            run = db.get(BuildRun, self.run.id)
            run.error = DISPATCH_ERROR_PREFIX + "enqueue failed"
            db.commit()
            claim_code_engineer_task(db, self.owner, self.project.id, self.run.run_id, task.task_id)
            status = get_requirements_status(db, self.owner, self.project.id)
            self.assertEqual(status.state, "engineering_running")
            self.assertIsNone(status.error)

    def test_provider_failures_cannot_reset_spent_turn_budget(self):
        with patch.object(
            leader_agent, "chat_with_tools", side_effect=ProviderException("busy", retryable=True)
        ) as chat:
            for _ in range(leader_agent.LEADER_SIMPLE_DISPATCH_TURNS):
                with self.assertRaises(ProviderException):
                    self._dispatch()
            with self.assertRaisesRegex(BusinessException, "预算"):
                self._dispatch()
            self.assertEqual(chat.call_count, leader_agent.LEADER_SIMPLE_DISPATCH_TURNS)

    def test_late_provider_error_cannot_overwrite_newer_dispatch_status(self):
        from app.services.requirements import _dispatch_approved_delivery

        def displaced_provider(**_kwargs):
            with self.session_factory() as db:
                saved = db.get(LeaderDispatchCheckpoint, self.approved_id)
                saved.lease_expires_at = utc_now() - timedelta(seconds=1)
                run = db.get(BuildRun, self.run.id)
                run.error = "newer dispatch status"
                db.commit()
                claim_dispatch(
                    db,
                    self.owner,
                    self.project.id,
                    self.run.run_id,
                    self.approved_id,
                    frozen_input={},
                )
            raise ProviderException("old provider response", retryable=True)

        with patch.object(leader_agent, "chat_with_tools", side_effect=displaced_provider):
            with self.session_factory() as db, self.assertRaises(ConflictException):
                _dispatch_approved_delivery(
                    db,
                    self.owner,
                    self.project.id,
                    self.run.run_id,
                    self.approved_id,
                )
        with self.session_factory() as db:
            self.assertEqual(db.get(BuildRun, self.run.id).error, "newer dispatch status")
            self.assertIsNotNone(db.get(LeaderDispatchCheckpoint, self.approved_id).lease_owner)

    def test_overlapping_and_expired_owners_are_fenced(self):
        args = (self.owner, self.project.id, self.run.run_id, self.approved_id)
        with self.session_factory() as db:
            _, old = claim_dispatch(db, *args, frozen_input={"original": True})
        with self.session_factory() as db:
            with self.assertRaisesRegex(ConflictException, "正在分派"):
                claim_dispatch(db, *args, frozen_input={"new": True})
            saved = db.get(LeaderDispatchCheckpoint, self.approved_id)
            saved.lease_expires_at = utc_now() - timedelta(seconds=1)
            db.commit()
            saved, current = claim_dispatch(db, *args, frozen_input={"new": True})
            self.assertEqual(saved.frozen_input, {"original": True})
            with self.assertRaises(ConflictException):
                save_dispatch(db, *args, old, {"turns": 99})
            release_dispatch(db, self.approved_id, old)
            db.refresh(saved)
            self.assertEqual(saved.lease_owner, current)
            save_dispatch(db, *args, current, {"turns": 2})

    def test_pause_resume_and_terminal_run_reject_old_checkpoint_writes(self):
        args = (self.owner, self.project.id, self.run.run_id, self.approved_id)
        with self.session_factory() as db:
            _, owner = claim_dispatch(db, *args, frozen_input={})
            pause_jobs_for_run(db, self.run.run_id, commit=True)
            with self.assertRaises(ConflictException):
                save_dispatch(db, *args, owner, {"turns": 1})
            _, owner = claim_dispatch(db, *args, frozen_input={})
            run = db.get(BuildRun, self.run.id)
            run.status, run.active_slot = "cancelled", None
            db.commit()
            with self.assertRaises(ConflictException):
                save_dispatch(db, *args, owner, {"turns": 2})
