"""Run one assigned Test Engineer task against an exact code identity."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.agents.test_engineer import TEST_ENGINEER_BUDGET_EXHAUSTED, verify_code
from app.core.exceptions import AppException, BusinessException, ConflictException
from app.models.configuration_item import ConfigurationItem
from app.models.task_execution import TaskExecution
from app.models.user import User
from app.schemas.test_report import TestReport
from app.services import test_engineer as test_engineer_service
from app.services.qa_verification import (
    can_extend_qa_execution_budget,
    qa_execution_budget_exhausted,
)
from app.services.source_artifact import ensure_code_workspace
from app.services.task_execution import fail_execution, renew_execution_lease, save_execution_draft
from app.telemetry.agent import agent_telemetry_scope

logger = logging.getLogger("forgeai")


def _record_quality_activity(
    db: Session,
    execution: TaskExecution,
    *,
    name: str,
    label: str,
    detail: str,
    ok: bool = True,
) -> None:
    draft = dict(execution.draft or {})
    checkpoint = draft.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("kind") != "quality_checkpoint":
        checkpoint = {"kind": "quality_checkpoint", "activity": []}
    activity = list(checkpoint.get("activity") or [])
    activity.append(
        {
            "id": f"{name}_{len(activity) + 1}",
            "name": name,
            "label": label,
            "detail": detail[:200],
            "ok": ok,
        }
    )
    checkpoint["activity"] = activity
    draft["checkpoint"] = checkpoint
    save_execution_draft(db, execution, draft)


def _saved_quality_progress(execution: TaskExecution) -> object:
    draft = execution.draft if isinstance(execution.draft, dict) else None
    checkpoint = draft.get("checkpoint") if draft else None
    if not isinstance(checkpoint, dict) or checkpoint.get("kind") != "quality_checkpoint":
        return None
    return checkpoint.get("verification")


def _save_quality_progress(
    db: Session,
    execution: TaskExecution,
    progress: dict[str, Any],
) -> None:
    """Persist resumable QA evidence and renew the execution fence atomically."""

    draft = dict(execution.draft or {})
    checkpoint = draft.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("kind") != "quality_checkpoint":
        checkpoint = {"kind": "quality_checkpoint", "activity": []}
    checkpoint["verification"] = progress
    draft["checkpoint"] = checkpoint
    save_execution_draft(db, execution, draft)


def run_test_engineer_task(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    recovery_execution_id: str | None = None,
    manual_budget_extension: bool = False,
    allow_execution_budget_extension: bool = False,
    on_execution_started: Callable[[TaskExecution], None] | None = None,
) -> ConfigurationItem:
    """Verify code read-only, then publish the immutable quality report."""

    previous = db.get(TaskExecution, recovery_execution_id) if recovery_execution_id else None
    if manual_budget_extension:
        if (
            previous is None
            or previous.task_id != task_id
            or previous.status != "failed"
            or previous.error != TEST_ENGINEER_BUDGET_EXHAUSTED
        ):
            raise ConflictException("只有预算耗尽的 QA 执行可显式扩额续接")

    eligible_previous = (
        allow_execution_budget_extension
        and previous is not None
        and previous.task_id == task_id
        and previous.status == "failed"
    )
    prior_progress = _saved_quality_progress(previous) if eligible_previous and previous else None
    if (
        eligible_previous
        and qa_execution_budget_exhausted(prior_progress)
        and not (can_extend_qa_execution_budget(prior_progress))
    ):
        raise ConflictException("QA 独立执行次数已达到显式续额上限")
    manual_execution_extension = eligible_previous and can_extend_qa_execution_budget(
        prior_progress
    )

    task, execution = test_engineer_service.claim_test_engineer_task(
        db,
        user,
        project_id,
        run_id,
        task_id,
        recovery_execution_id=recovery_execution_id,
    )
    if on_execution_started is not None:
        on_execution_started(execution)
    try:
        _record_quality_activity(
            db,
            execution,
            name="quality_start",
            label="开始独立验证",
            detail="检查准确代码结果并逐条验证批准的验收条件",
        )
        with Session(bind=db.get_bind(), autoflush=False) as reader:
            inputs = test_engineer_service.load_test_inputs(
                reader,
                project_id,
                run_id,
                task.input_configuration_item_ids[0],
            )
        workspace_root, _ = ensure_code_workspace(
            db,
            project_id=project_id,
            run_id=run_id,
            code_item_id=inputs.code_item.item_id,
        )

        def heartbeat() -> None:
            renew_execution_lease(db, task_id, execution.execution_id)

        def save_progress(progress: dict[str, Any]) -> None:
            _save_quality_progress(db, execution, progress)

        with agent_telemetry_scope(
            project_id=project_id,
            build_run_id=run_id,
            task_id=task_id,
            execution_id=execution.execution_id,
            role="Test Engineer",
            bind=db.get_bind(),
        ):
            report = verify_code(
                spec=inputs.app_spec,
                system_design=inputs.system_design,
                code_item_id=inputs.code_item.item_id,
                code_source_hash=inputs.code_artifact.source_hash,
                acceptance_test_plan_item_id=(
                    inputs.acceptance_test_plan_item.item_id
                    if inputs.acceptance_test_plan_item is not None
                    else None
                ),
                acceptance_test_hash=(
                    inputs.acceptance_test_plan_item.content_hash
                    if inputs.acceptance_test_plan_item is not None
                    else None
                ),
                acceptance_test_plan=inputs.acceptance_test_plan,
                workspace_root=workspace_root,
                saved_progress=_saved_quality_progress(execution),
                manual_budget_extension_attempt=(
                    execution.attempt if manual_budget_extension else None
                ),
                manual_execution_extension_attempt=(
                    execution.attempt if manual_execution_extension else None
                ),
                inherited_suite=inputs.inherited_suite,
                heartbeat=heartbeat,
                on_progress=save_progress,
            )
        _record_quality_activity(
            db,
            execution,
            name="quality_result",
            label="独立验证完成",
            detail=report.summary,
            ok=report.quality_conclusion.value == "passed",
        )
        report_item = test_engineer_service.complete_test_engineer_task(
            db,
            user,
            project_id,
            run_id,
            task_id,
            execution.execution_id,
            report,
        )
    except Exception as exc:
        message = str(exc).strip() or "Test Engineer 执行失败，可重试恢复。"
        db.rollback()
        try:
            _record_quality_activity(
                db,
                execution,
                name="quality_error",
                label="独立验证失败",
                detail=message,
                ok=False,
            )
        except AppException:
            db.rollback()
        fail_execution(
            db,
            task_id,
            execution.execution_id,
            error=message[:500],
        )
        raise

    route_completed_test_report(db, user, project_id, run_id, report_item)
    return report_item


def route_completed_test_report(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    report_item: ConfigurationItem,
) -> None:
    """Idempotently apply deterministic routing for one immutable quality report."""

    report = TestReport.model_validate(report_item.payload)
    if report.quality_conclusion.value == "failed":
        # Test Engineer remains read-only; Leader consumes the immutable report.
        from app.orchestration.leader import start_dispatched_delivery
        from app.services import leader as leader_service

        try:
            repair_task = leader_service.dispatch_quality_repair(
                db,
                user,
                project_id,
                run_id,
                report_item.item_id,
            )
            if repair_task is None:
                return
            if repair_task.recipient == "Code Engineer":
                start_dispatched_delivery(db, user, project_id, run_id, repair_task)
            else:
                raise BusinessException("质量失败只能派给 Code Engineer")
        except Exception:
            # The immutable report is already published. Never rewrite that
            # successful execution as failed because downstream routing broke.
            logger.exception(
                "quality repair routing failed report_item_id=%s",
                report_item.item_id,
            )
            raise
