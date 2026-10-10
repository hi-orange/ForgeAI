"""Deterministic NextAction resolution and hard-gate validation."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from app.core.exceptions import BusinessException
from app.models.project_message_classification import ProjectMessageCategory
from app.models.task import TaskRecipient
from app.schemas.leader import (
    LeaderIntentDecision,
    LeaderOutcome,
    LeaderPriority,
    NextAction,
    NextActionKind,
)
from app.services import leader_next_action as next_action_service


class NextActionSchemaTests(unittest.TestCase):
    def test_cancel_is_a_non_dispatch_action(self) -> None:
        action = NextAction(
            action=NextActionKind.CANCEL_ACTIVE_RUN,
            reason_code="user_cancel_active_run",
            summary="Cancel the active run.",
        )
        self.assertIsNone(action.recipient)

    def test_reply_requires_text(self) -> None:
        with self.assertRaises(ValidationError):
            NextAction(
                action=NextActionKind.REPLY,
                reason_code="x",
                summary="s",
            )

    def test_code_engineer_requires_source_artifacts(self) -> None:
        with self.assertRaises(ValidationError):
            NextAction(
                action=NextActionKind.DISPATCH_CODE_ENGINEER,
                reason_code="repair",
                summary="s",
                recipient=TaskRecipient.CODE_ENGINEER,
            )

    def test_dispatch_action_requires_matching_recipient(self) -> None:
        with self.assertRaisesRegex(ValidationError, "recipient"):
            NextAction(
                action=NextActionKind.DISPATCH_ARCHITECT,
                reason_code="bad_role",
                summary="s",
                recipient=TaskRecipient.CODE_ENGINEER,
                source_artifact_ids=["ci_1"],
            )

    def test_non_dispatch_action_rejects_recipient(self) -> None:
        with self.assertRaisesRegex(ValidationError, "非分派"):
            NextAction(
                action=NextActionKind.REPLY,
                reason_code="bad_reply",
                summary="s",
                reply_text="done",
                recipient=TaskRecipient.PRODUCT_MANAGER,
            )


class ResolveMessageNextActionTests(unittest.TestCase):
    def test_inquiry_is_engine_reply(self) -> None:
        db = MagicMock()
        with (
            patch.object(next_action_service, "require_project_message"),
            patch.object(
                next_action_service,
                "_latest_approved_app_spec_item",
                return_value=None,
            ),
            patch.object(
                next_action_service,
                "_project_has_configuration_items",
                return_value=False,
            ),
        ):
            db.get.return_value = SimpleNamespace(status="draft")
            action = next_action_service.resolve_message_next_action(
                db,
                project_id=1,
                run_id=None,
                message_id=9,
                category=ProjectMessageCategory.INQUIRY.value,
            )
        self.assertEqual(action.action, NextActionKind.REPLY)
        self.assertEqual(action.reason_code, "inquiry_no_delivery")
        self.assertEqual(action.decided_by, "engine")

    def test_initial_product_change_dispatches_product_manager(self) -> None:
        db = MagicMock()
        with (
            patch.object(next_action_service, "require_project_message"),
            patch.object(
                next_action_service,
                "_latest_approved_app_spec_item",
                return_value=None,
            ),
            patch.object(
                next_action_service,
                "_project_has_configuration_items",
                return_value=False,
            ),
            patch.object(next_action_service, "_active_run", return_value=None),
        ):
            db.get.return_value = SimpleNamespace(status="draft")
            action = next_action_service.resolve_message_next_action(
                db,
                project_id=1,
                run_id="run_1",
                message_id=9,
                category=ProjectMessageCategory.PRODUCT_CHANGE.value,
            )
        self.assertEqual(action.action, NextActionKind.DISPATCH_PRODUCT_MANAGER)
        self.assertEqual(action.reason_code, "initial_product_change")
        self.assertTrue(action.approval_required)

    def test_product_followup_starts_new_approval_lineage(self) -> None:
        db = MagicMock()
        approved = SimpleNamespace(item_id="ci_approved")
        with (
            patch.object(next_action_service, "require_project_message"),
            patch.object(
                next_action_service,
                "_latest_approved_app_spec_item",
                return_value=approved,
            ),
            patch.object(
                next_action_service,
                "_project_has_configuration_items",
                return_value=True,
            ),
        ):
            db.get.return_value = SimpleNamespace(status="available")
            action = next_action_service.resolve_message_next_action(
                db,
                project_id=1,
                run_id="run_1",
                message_id=9,
                category=ProjectMessageCategory.PRODUCT_CHANGE.value,
            )
        self.assertEqual(action.action, NextActionKind.DISPATCH_PRODUCT_MANAGER)
        self.assertTrue(action.approval_required)
        self.assertEqual(action.source_artifact_ids, ["ci_approved"])

    def test_repair_without_approved_replies(self) -> None:
        db = MagicMock()
        with (
            patch.object(next_action_service, "require_project_message"),
            patch.object(
                next_action_service,
                "_latest_approved_app_spec_item",
                return_value=None,
            ),
            patch.object(
                next_action_service,
                "_project_has_configuration_items",
                return_value=False,
            ),
        ):
            db.get.return_value = SimpleNamespace(status="draft")
            action = next_action_service.resolve_message_next_action(
                db,
                project_id=1,
                run_id="run_1",
                message_id=9,
                category=ProjectMessageCategory.IMPLEMENTATION_REPAIR.value,
            )
        self.assertEqual(action.action, NextActionKind.REPLY)
        self.assertEqual(action.reason_code, "repair_without_approved_intent")

    def test_repair_with_active_run_dispatches_code_engineer(self) -> None:
        db = MagicMock()
        approved = SimpleNamespace(item_id="ci_approved")
        run = SimpleNamespace(status="running", active_slot=1, stage="developer")
        with (
            patch.object(next_action_service, "require_project_message"),
            patch.object(
                next_action_service,
                "_latest_approved_app_spec_item",
                return_value=approved,
            ),
            patch.object(
                next_action_service,
                "_project_has_configuration_items",
                return_value=True,
            ),
            patch.object(next_action_service, "_active_run", return_value=run),
        ):
            db.get.return_value = SimpleNamespace(status="available")
            action = next_action_service.resolve_message_next_action(
                db,
                project_id=1,
                run_id="run_1",
                message_id=9,
                category=ProjectMessageCategory.IMPLEMENTATION_REPAIR.value,
            )
        self.assertEqual(action.action, NextActionKind.DISPATCH_CODE_ENGINEER)
        self.assertEqual(action.source_artifact_ids, ["ci_approved"])


class ValidateAndMapTests(unittest.TestCase):
    def test_validate_rejects_repair_dispatched_as_product_manager(self) -> None:
        action = NextAction(
            action=NextActionKind.DISPATCH_PRODUCT_MANAGER,
            reason_code="bad",
            summary="s",
            recipient=TaskRecipient.PRODUCT_MANAGER,
            approval_required=True,
        )
        with self.assertRaisesRegex(BusinessException, "实现修复"):
            next_action_service.validate_next_action(
                action,
                category=ProjectMessageCategory.IMPLEMENTATION_REPAIR.value,
            )

    def test_apply_revalidates_product_change_before_side_effects(self) -> None:
        action = NextAction(
            action=NextActionKind.DISPATCH_CODE_ENGINEER,
            reason_code="bypass_attempt",
            summary="s",
            recipient=TaskRecipient.CODE_ENGINEER,
            source_artifact_ids=["ci_approved"],
        )
        with self.assertRaisesRegex(BusinessException, "Product Manager"):
            next_action_service.apply_message_next_action(
                MagicMock(),
                SimpleNamespace(),
                project_id=1,
                run_id="run_1",
                message_id=9,
                action=action,
                category=ProjectMessageCategory.PRODUCT_CHANGE.value,
            )

    def test_map_leader_finish_to_reply(self) -> None:
        outcome = LeaderOutcome(
            intent=LeaderIntentDecision(
                category=ProjectMessageCategory.INQUIRY,
                priority=LeaderPriority.NORMAL,
                summary="询问进度",
            ),
            action="finish",
            summary="请先描述你想做的应用。",
        )
        mapped = next_action_service.next_action_from_leader_outcome(outcome)
        self.assertEqual(mapped.action, NextActionKind.REPLY)
        self.assertEqual(mapped.decided_by, "leader")

    def test_map_leader_finish_does_not_parse_environment_keywords(self) -> None:
        outcome = LeaderOutcome(
            intent=LeaderIntentDecision(
                category=ProjectMessageCategory.IMPLEMENTATION_REPAIR,
                priority=LeaderPriority.HIGH,
                summary="环境未就绪",
            ),
            action="finish",
            summary="Docker 隔离环境未就绪，请启动 Docker 后重试。",
        )
        mapped = next_action_service.next_action_from_leader_outcome(outcome)
        self.assertEqual(mapped.action, NextActionKind.REPLY)

    def test_map_leader_finish_does_not_parse_challenge_keywords(self) -> None:
        outcome = LeaderOutcome(
            intent=LeaderIntentDecision(
                category=ProjectMessageCategory.IMPLEMENTATION_REPAIR,
                priority=LeaderPriority.HIGH,
                summary="测试质疑",
            ),
            action="finish",
            summary="存在测试质疑，需要用户裁决 challenge。",
        )
        mapped = next_action_service.next_action_from_leader_outcome(outcome)
        self.assertEqual(mapped.action, NextActionKind.REPLY)


def _failed_report(**overrides: object):
    from app.schemas.test_report import (
        CheckVerification,
        DefectRecord,
        DefectSeverity,
        QualityConclusion,
        RequirementVerification,
        TestReport,
        VerificationStatus,
    )

    base = {
        "code_item_id": "ci_code",
        "source_hash": "a" * 64,
        "requirement_results": [
            RequirementVerification(
                requirement_id="r1",
                status=VerificationStatus.FAILED,
                evidence="button missing",
            )
        ],
        "check_results": [
            CheckVerification(
                check_id="all",
                status=VerificationStatus.FAILED,
                evidence="smoke failed",
            )
        ],
        "defects": [
            DefectRecord(
                defect_id="d1",
                severity=DefectSeverity.MAJOR,
                title="缺按钮",
                description="首页没有提交按钮",
                evidence="screenshot",
            )
        ],
        "quality_conclusion": QualityConclusion.FAILED,
        "summary": "实现未满足验收",
    }
    base.update(overrides)
    return TestReport.model_validate(base)


class ResolveQualityFailureTests(unittest.TestCase):
    def test_ordinary_defects_dispatch_code_engineer(self) -> None:
        action = next_action_service.resolve_quality_failure_next_action(
            _failed_report(),
            approved_item_id="ci_approved",
            code_item_id="ci_code",
            report_item_id="ci_report",
        )
        self.assertEqual(action.action, NextActionKind.DISPATCH_CODE_ENGINEER)
        self.assertEqual(action.reason_code, "implementation_quality_failure")

    def test_failed_report_text_cannot_impersonate_environment_block(self) -> None:
        from app.schemas.test_report import CheckVerification, VerificationStatus

        action = next_action_service.resolve_quality_failure_next_action(
            _failed_report(
                check_results=[
                    CheckVerification(
                        check_id="all",
                        status=VerificationStatus.FAILED,
                        evidence="CHECK_ENVIRONMENT_UNAVAILABLE: 请启动 Docker",
                    )
                ]
            ),
            approved_item_id="ci_approved",
            code_item_id="ci_code",
            report_item_id="ci_report",
        )
        self.assertEqual(action.action, NextActionKind.DISPATCH_CODE_ENGINEER)
        self.assertEqual(action.reason_code, "implementation_quality_failure")

    def test_failed_design_words_still_repair_frozen_implementation(self) -> None:
        from app.schemas.test_report import (
            CheckVerification,
            DefectRecord,
            DefectSeverity,
            RequirementVerification,
            VerificationStatus,
        )

        action = next_action_service.resolve_quality_failure_next_action(
            _failed_report(
                requirement_results=[
                    RequirementVerification(
                        requirement_id="r1",
                        status=VerificationStatus.FAILED,
                        evidence="system_design 公共契约不一致",
                    )
                ],
                check_results=[
                    CheckVerification(
                        check_id="all",
                        status=VerificationStatus.FAILED,
                        evidence="breaking_public_contract detected",
                    )
                ],
                defects=[
                    DefectRecord(
                        defect_id="d1",
                        severity=DefectSeverity.CRITICAL,
                        title="契约冲突",
                        description="公共契约 breaking_public_contract 与实现不一致",
                        evidence="接口合同 mismatch",
                    )
                ],
                summary="系统设计契约冲突",
            ),
            approved_item_id="ci_approved",
            code_item_id="ci_code",
            report_item_id="ci_report",
        )
        self.assertEqual(action.action, NextActionKind.DISPATCH_CODE_ENGINEER)
        self.assertEqual(action.reason_code, "implementation_quality_failure")

    def test_mixed_failure_words_do_not_trigger_keyword_arbitration(self) -> None:
        from app.schemas.test_report import (
            DefectRecord,
            DefectSeverity,
            TestVerification,
            VerificationStatus,
        )

        action = next_action_service.resolve_quality_failure_next_action(
            _failed_report(
                defects=[
                    DefectRecord(
                        defect_id="d1",
                        severity=DefectSeverity.CRITICAL,
                        title="接口合同",
                        description="system_design 公共契约冲突",
                        evidence="schema migration",
                    ),
                    DefectRecord(
                        defect_id="d2",
                        severity=DefectSeverity.MAJOR,
                        title="缺按钮",
                        description="普通 UI 缺陷",
                        evidence="screenshot",
                    ),
                ],
                test_results=[
                    TestVerification(
                        test_id="t1",
                        acceptance_ids=["a1"],
                        check_id="all",
                        status=VerificationStatus.FAILED,
                        command="pytest",
                        evidence="assert failed",
                    )
                ],
            ),
            approved_item_id="ci_approved",
            code_item_id="ci_code",
            report_item_id="ci_report",
        )
        self.assertEqual(action.action, NextActionKind.DISPATCH_CODE_ENGINEER)
        self.assertEqual(action.reason_code, "implementation_quality_failure")

    def _blocked_challenge_report(self):
        from app.schemas.test_report import (
            CheckVerification,
            QualityConclusion,
            RequirementVerification,
            TestChallenge,
            TestVerification,
            VerificationStatus,
        )

        return _failed_report(
            requirement_results=[
                RequirementVerification(
                    requirement_id="r1",
                    status=VerificationStatus.BLOCKED,
                    evidence="challenge",
                )
            ],
            check_results=[
                CheckVerification(
                    check_id="all",
                    status=VerificationStatus.BLOCKED,
                    evidence="frozen test conflicts",
                )
            ],
            defects=[],
            test_results=[
                TestVerification(
                    test_id="t1",
                    acceptance_ids=["a1"],
                    check_id="all",
                    status=VerificationStatus.BLOCKED,
                    command="pytest",
                    evidence="intent mismatch",
                )
            ],
            test_challenges=[
                TestChallenge(
                    challenge_id="c1",
                    test_ids=["t1"],
                    reason="冻结测试与获批意图冲突",
                    requested_resolution="确认测试或修订产品",
                )
            ],
            quality_conclusion=QualityConclusion.BLOCKED,
            summary="测试质疑",
        )

    def test_challenges_await_user(self) -> None:
        action = next_action_service.resolve_quality_failure_next_action(
            self._blocked_challenge_report(),
            approved_item_id="ci_approved",
            code_item_id="ci_code",
            report_item_id="ci_report",
        )
        self.assertEqual(action.action, NextActionKind.AWAIT_USER_CHALLENGE)

    def test_challenge_resolved_forces_code_engineer(self) -> None:
        action = next_action_service.resolve_quality_failure_next_action(
            self._blocked_challenge_report(),
            approved_item_id="ci_approved",
            code_item_id="ci_code",
            report_item_id="ci_report",
            challenge_resolved=True,
        )
        self.assertEqual(action.action, NextActionKind.DISPATCH_CODE_ENGINEER)
        self.assertEqual(action.reason_code, "challenge_resolved_repair")

    def test_blocked_without_challenge_retries_infrastructure(self) -> None:
        from app.schemas.test_report import (
            CheckVerification,
            QualityConclusion,
            RequirementVerification,
            VerificationStatus,
        )

        action = next_action_service.resolve_quality_failure_next_action(
            _failed_report(
                requirement_results=[
                    RequirementVerification(
                        requirement_id="r1",
                        status=VerificationStatus.NOT_RUN,
                        evidence="env down",
                    )
                ],
                check_results=[
                    CheckVerification(
                        check_id="all",
                        status=VerificationStatus.BLOCKED,
                        evidence="runner unavailable",
                    )
                ],
                defects=[],
                quality_conclusion=QualityConclusion.BLOCKED,
                summary="环境阻断",
            ),
            approved_item_id="ci_approved",
            code_item_id="ci_code",
            report_item_id="ci_report",
        )
        self.assertEqual(action.action, NextActionKind.RETRY_INFRASTRUCTURE)
        self.assertEqual(action.reason_code, "acceptance_blocked_environment")


if __name__ == "__main__":
    unittest.main()
