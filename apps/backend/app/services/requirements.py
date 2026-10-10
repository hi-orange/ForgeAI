from __future__ import annotations

import logging

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, ConflictException
from app.core.settings import settings
from app.generation.workspace import default_workspace_path, workspace_is_ready
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.project_message import ProjectMessage, ProjectMessageSender
from app.models.project_message_classification import ProjectMessageCategory
from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.models.task_result import TaskResult
from app.models.user import User
from app.orchestration.code_engineer import EXECUTION_MODE
from app.orchestration.leader import run_architect_and_continue, start_dispatched_delivery
from app.orchestration.product_manager import run_product_manager_workflow
from app.orchestration.test_engineer import run_test_engineer_task
from app.schemas.leader import NextActionKind
from app.schemas.product_manager_workflow import (
    ProductManagerWorkflowOutcome,
    ProductManagerWorkflowResult,
)
from app.schemas.project_message import ProjectMessageCreate
from app.schemas.requirements import (
    EngineeringActivity,
    QualityChallengeResolution,
    RequirementsApproval,
    RequirementsStatus,
)
from app.schemas.test_report import QualityConclusion, TestReport
from app.services import build_run as build_run_service
from app.services import engineering
from app.services import leader as leader_service
from app.services import leader_next_action as leader_next_action_service
from app.services import project as project_service
from app.services import project_message as project_message_service
from app.services import project_message_classification as classification_service
from app.services import run_revision as run_revision_service
from app.services import task as task_service
from app.services.app_spec import approve_requirements as persist_approved_requirements
from app.services.app_spec import read_app_spec
from app.services.engineering import (
    APPROVAL_VERSION,
    ARCHITECTURE_TASK_KEY,
    ENGINEERING_TASK_KEY,
    QUALITY_TASK_KEY,
    find_architecture_task,
    find_pending_engineering_task,
    load_approved_app_spec,
    load_engineering_source,
    mark_engineering_inactive,
    read_frozen_input_snapshot,
)
from app.services.leader import create_clarification_plan, create_product_revision_plan
from app.services.task_execution import fail_execution, latest_execution, lock_run, utc_now
from app.services.test_engineer import load_test_inputs

logger = logging.getLogger("forgeai")

WRITABLE_STATES = frozenset(
    {
        "not_started",
        "needs_user_input",
        "awaiting_approval",
        "completed",
        "stopped",
        "quality_failed",
    }
)
CONTINUE_PM_STATES = frozenset({"pending", "retry_available", "ready_for_delivery"})


# ---------------------------------------------------------------------------
# Coarse commands used by the requirements API
# ---------------------------------------------------------------------------


def submit_requirements(
    db: Session,
    user: User,
    project_id: int,
    content: str,
    client_message_id: str,
) -> RequirementsStatus:
    """Persist one user turn and advance the matching requirements workflow."""

    payload = ProjectMessageCreate(content=content, client_message_id=client_message_id)
    replay = project_message_service.find_idempotent_user_message(db, user, project_id, payload)
    if replay is not None:
        if _message_has_plan(db, project_id, replay.id):
            return get_requirements_status(db, user, project_id)
        return start_from_message(db, user, project_id, replay.id)

    status = get_requirements_status(db, user, project_id)
    if status.state not in WRITABLE_STATES:
        raise BusinessException("当前状态不能提交消息")

    item_id = status.result.configuration_item_id if status.result else None
    if status.state in ("needs_user_input", "awaiting_approval") and status.run_id and item_id:
        plan = create_clarification_plan(
            db,
            user,
            project_id,
            status.run_id,
            item_id,
            payload,
        )
        run_product_manager_workflow(db, user, project_id, status.run_id, plan.cause_message_id)
        return get_requirements_status(db, user, project_id)

    message = project_message_service.create_user_project_message(
        db,
        user,
        project_id,
        payload,
    )
    return start_from_message(db, user, project_id, message.id)


def start_requirements(
    db: Session,
    user: User,
    project_id: int,
    message_id: int | None = None,
) -> RequirementsStatus:
    """Start the initial saved project prompt without posting it a second time."""

    status = get_requirements_status(db, user, project_id)
    if status.state != "not_started":
        return status
    if message_id is None:
        initial = _first_user_message(db, project_id)
        if initial is None:
            return status
        message_id = initial.id
    return start_from_message(db, user, project_id, message_id)


def start_from_message(
    db: Session,
    user: User,
    project_id: int,
    message_id: int,
) -> RequirementsStatus:
    """Classify a user turn, resolve NextAction, then let the workflow engine apply it."""

    classification = classification_service.classify_user_message(db, user, project_id, message_id)
    status = get_requirements_status(db, user, project_id)
    needs_run = classification.category in {
        ProjectMessageCategory.PRODUCT_CHANGE.value,
        ProjectMessageCategory.IMPLEMENTATION_REPAIR.value,
    }
    run_id = (
        _ensure_run_id(
            db,
            user,
            project_id,
            status,
            message_id=message_id,
            category=classification.category,
        )
        if needs_run
        else status.run_id
    )
    action = leader_next_action_service.decide_message_next_action(
        db,
        user,
        project_id=project_id,
        run_id=run_id,
        message_id=message_id,
        category=classification.category,
    )
    if action.action == NextActionKind.DISPATCH_PRODUCT_MANAGER and run_id:
        leader_next_action_service.apply_message_next_action(
            db,
            user,
            project_id=project_id,
            run_id=run_id,
            message_id=message_id,
            action=action,
            category=classification.category,
        )
        run_product_manager_workflow(db, user, project_id, run_id, message_id)
        return get_requirements_status(db, user, project_id)

    if run_id is None and action.action in {
        NextActionKind.DISPATCH_CODE_ENGINEER,
        NextActionKind.DISPATCH_ARCHITECT,
        NextActionKind.DISPATCH_PRODUCT_MANAGER,
    }:
        raise BusinessException("交付分派需要有效的 BuildRun")

    task = None
    if run_id is not None:
        task = leader_next_action_service.apply_message_next_action(
            db,
            user,
            project_id=project_id,
            run_id=run_id,
            message_id=message_id,
            action=action,
            category=classification.category,
        )
    elif action.action in {NextActionKind.REPLY, NextActionKind.ASK_USER}:
        # No run yet: still persist guidance/question on the project timeline.
        text = action.question if action.action == NextActionKind.ASK_USER else action.reply_text
        assert text is not None
        project_message_service.create_assistant_project_message(
            db,
            user,
            project_id,
            text,
            client_message_id=f"leader:{action.reason_code}:msg:{message_id}",
        )
    if task is not None:
        assert run_id is not None
        start_dispatched_delivery(db, user, project_id, run_id, task)
    return get_requirements_status(db, user, project_id)


def resolve_quality_challenge(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    report_item_id: str,
    resolution: QualityChallengeResolution,
) -> RequirementsStatus:
    """Resolve one exact blocked report through an explicit user-owned branch."""

    resolution = QualityChallengeResolution.model_validate(resolution.model_dump())
    status = get_requirements_status(db, user, project_id)
    if status.run_id != run_id:
        raise ConflictException("构建任务不匹配")
    if status.state != "quality_challenge":
        if resolution.action == "revise_product":
            assert resolution.client_message_id is not None
            assert resolution.content is not None
            replay = project_message_service.find_idempotent_user_message(
                db,
                user,
                project_id,
                ProjectMessageCreate(
                    content=resolution.content,
                    client_message_id=resolution.client_message_id,
                ),
            )
            if replay is not None:
                return status
        elif status.task_id is not None:
            current_task = task_service.get_user_task(db, user, project_id, status.task_id)
            if (
                current_task.recipient == "Code Engineer"
                and report_item_id in current_task.input_configuration_item_ids
            ):
                return status
        raise ConflictException("当前没有待仲裁的测试质疑")
    if status.test_report_item_id != report_item_id or not status.code_item_id:
        raise ConflictException("测试质疑报告已变化，请刷新后重试")

    run = lock_run(db, user, project_id, run_id)
    if (run.status, run.stage, run.active_slot) != ("failed", "qa", None):
        raise ConflictException("测试质疑已经被处理，请刷新进度")
    inputs = load_test_inputs(db, project_id, run_id, status.code_item_id, lock=True)
    source = load_engineering_source(
        db,
        project_id,
        run_id,
        inputs.code_item.upstream_item_ids[0],
        lock=True,
    )

    if resolution.action == "repair_code":
        if not status.retryable:
            raise ConflictException("代码自动修复轮次已用尽，请修改产品意图或重新开始构建")
        run.status = "running"
        run.stage = "qa"
        run.active_slot = 1
        run.error = None
        db.flush()
        task = leader_service.dispatch_quality_repair(
            db,
            user,
            project_id,
            run_id,
            report_item_id,
            challenge_resolved=True,
        )
        if task is not None:
            start_dispatched_delivery(db, user, project_id, run_id, task)
        return get_requirements_status(db, user, project_id)

    assert resolution.client_message_id is not None
    assert resolution.content is not None
    run.status = "running"
    run.stage = "pm"
    run.active_slot = 1
    run.error = None
    db.flush()
    plan = create_product_revision_plan(
        db,
        user,
        project_id,
        run_id,
        source.app_spec_item.item_id,
        report_item_id,
        ProjectMessageCreate(
            content=resolution.content,
            client_message_id=resolution.client_message_id,
        ),
    )
    run_product_manager_workflow(db, user, project_id, run_id, plan.cause_message_id)
    return get_requirements_status(db, user, project_id)


def continue_requirements(db: Session, user: User, project_id: int) -> RequirementsStatus:
    """Resume whichever saved PM or engineering step currently owns the BuildRun."""

    status = get_requirements_status(db, user, project_id)
    if not status.run_id:
        raise BusinessException("当前没有可继续的构建")
    if status.state == "retry_available" and not status.retryable:
        raise ConflictException(status.error or "相同错误已达到自动修复上限")

    # Continue is a coarse, user-facing action and can race with automatic dispatch or
    # polling. Once Architect / QA already owns an active execution, a duplicate request
    # is successful and idempotent rather than an invalid transition.
    if status.state in {"design_running", "quality_running"}:
        return status

    if status.state == "quality_failed" and status.retryable and status.test_report_item_id:
        task = leader_service.retry_blocked_quality_validation(
            db,
            user,
            project_id,
            status.run_id,
            status.test_report_item_id,
        )
        run_test_engineer_task(db, user, project_id, status.run_id, task.task_id)
        return get_requirements_status(db, user, project_id)

    current_task = (
        task_service.get_user_task(db, user, project_id, status.task_id) if status.task_id else None
    )
    if (
        status.state == "engineering_running"
        or status.state == "design_pending"
        or status.state == "engineering_pending"
        or status.state == "engineering_generated"
        or status.state == "quality_pending"
        or (
            status.state == "retry_available"
            and current_task is not None
            and current_task.recipient != "Product Manager"
        )
    ):
        return continue_engineering(db, user, project_id, status.run_id)

    if status.state == "ready_for_delivery" and status.result:
        task = leader_service.dispatch_approved_requirements(
            db,
            user,
            project_id,
            status.run_id,
            status.result.configuration_item_id,
        )
        start_dispatched_delivery(db, user, project_id, status.run_id, task)
        return get_requirements_status(db, user, project_id)

    if status.state in CONTINUE_PM_STATES and status.message_id:
        recovery = status.execution_id if status.state == "retry_available" else None
        run_product_manager_workflow(
            db,
            user,
            project_id,
            status.run_id,
            status.message_id,
            recovery_execution_id=recovery,
        )
        return get_requirements_status(db, user, project_id)

    raise BusinessException("当前没有可继续的构建步骤")


def approve_requirement_plan(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    item_id: str,
    approval: RequirementsApproval,
) -> RequirementsStatus:
    """Persist explicit approval, freeze it as engineering input, and start delivery."""

    approved_id = persist_approved_requirements(db, user, project_id, run_id, item_id, approval)
    try:
        task = leader_service.dispatch_approved_requirements(
            db, user, project_id, run_id, approved_id
        )
        start_dispatched_delivery(db, user, project_id, run_id, task)
    except Exception as exc:
        # Approval is already committed. Keep the approved checkpoint recoverable so the
        # client can continue/dispatch without treating the whole approve call as lost.
        logger.exception("dispatch after approval failed project=%s run=%s", project_id, run_id)
        # A failed flush/commit leaves SQLAlchemy's Session unusable until it is rolled back.
        # The approval checkpoint was committed before dispatch, so rolling back here only
        # discards the incomplete dispatch attempt and lets us return a recoverable status.
        db.rollback()
        status = get_requirements_status(db, user, project_id)
        if status.state in {
            "ready_for_delivery",
            "design_pending",
            "design_running",
            "engineering_pending",
            "engineering_running",
            "quality_pending",
            "quality_running",
        }:
            detail = str(exc).strip() or exc.__class__.__name__
            status.error = f"计划已批准，但自动分派下一步失败：{detail[:400]}"
            return status
        raise
    return get_requirements_status(db, user, project_id)


def continue_engineering(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
) -> RequirementsStatus:
    status = get_requirements_status(db, user, project_id)
    if status.run_id != run_id:
        raise BusinessException("构建任务不匹配")
    current_task = (
        task_service.get_user_task(db, user, project_id, status.task_id) if status.task_id else None
    )
    if current_task is not None and current_task.recipient == "Architect":
        if status.state not in ("design_pending", "retry_available"):
            raise BusinessException("当前没有可继续的 Architect 任务")
        run_architect_and_continue(
            db,
            user,
            project_id,
            run_id,
            current_task.task_id,
            recovery_execution_id=(
                status.execution_id if status.state == "retry_available" else None
            ),
        )
        return get_requirements_status(db, user, project_id)
    if current_task is not None and current_task.recipient == "Code Engineer":
        if status.state == "engineering_pending":
            task, execution = engineering.claim_code_engineer_task(
                db, user, project_id, run_id, current_task.task_id
            )
            engineering.start_claimed_engineering(db, user, project_id, run_id, task, execution)
            return get_requirements_status(db, user, project_id)
        if status.state == "engineering_generated" and status.code_item_id:
            leader_service.dispatch_completed_code(
                db, user, project_id, run_id, status.code_item_id
            )
            return get_requirements_status(db, user, project_id)
        if status.state == "retry_available":
            return _resume_engineering_from_retry(db, user, project_id, run_id, status)
        if status.state == "engineering_running":
            return ensure_engineering_running(db, user, project_id, run_id, force=True)
        raise BusinessException("当前没有可继续的 Code Engineer 任务")
    if current_task is not None and current_task.recipient == "Test Engineer":
        if status.state not in ("quality_pending", "retry_available"):
            raise BusinessException("当前没有可继续的 Test Engineer 任务")
        try:
            run_test_engineer_task(
                db,
                user,
                project_id,
                run_id,
                current_task.task_id,
                recovery_execution_id=(
                    status.execution_id if status.state == "retry_available" else None
                ),
            )
        except BusinessException:
            # Task already recorded quality_error / failed execution; surface as paused status.
            return get_requirements_status(db, user, project_id)
        return get_requirements_status(db, user, project_id)
    raise BusinessException("当前没有可继续的工程任务")


def ensure_engineering_running(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    *,
    force: bool = False,
) -> RequirementsStatus:
    status = get_requirements_status(db, user, project_id)
    if status.run_id != run_id or status.state != "engineering_running":
        return status
    if not status.execution_id or engineering.is_engineering_active(status.execution_id):
        return status
    if status.error and not force:
        return status
    task_id = status.task_id
    if not task_id:
        return status
    try:
        task, execution = engineering.claim_code_engineer_task(
            db, user, project_id, run_id, task_id
        )
        engineering.start_claimed_engineering(db, user, project_id, run_id, task, execution)
    except ConflictException:
        return get_requirements_status(db, user, project_id)
    return get_requirements_status(db, user, project_id)


def _resume_engineering_from_retry(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    status: RequirementsStatus,
) -> RequirementsStatus:
    task_id = status.task_id
    if not task_id or not status.execution_id:
        raise BusinessException("当前没有可继续的工程任务")
    # A worker can publish its blocked checkpoint just before releasing the
    # in-process marker. Do not supersede it during that finalization window.
    if engineering.is_engineering_active(status.execution_id):
        return status
    task, execution = engineering.claim_code_engineer_task(
        db,
        user,
        project_id,
        run_id,
        task_id,
        recovery_execution_id=status.execution_id,
    )
    engineering.start_claimed_engineering(db, user, project_id, run_id, task, execution)
    return get_requirements_status(db, user, project_id)


def _ensure_run_id(
    db: Session,
    user: User,
    project_id: int,
    status: RequirementsStatus,
    *,
    message_id: int,
    category: str,
) -> str:
    if status.run_id:
        existing = db.scalar(select(BuildRun).where(BuildRun.run_id == status.run_id))
        if existing is not None and existing.status in {"queued", "running"}:
            return status.run_id
        return run_revision_service.create_revision_run(
            db,
            user,
            project_id,
            source_run_id=status.run_id,
            cause_message_id=message_id,
            kind=category,
        ).run_id
    try:
        return build_run_service.create_build_run(db, user, project_id).run_id
    except ConflictException:
        concurrent = get_requirements_status(db, user, project_id)
        if concurrent.run_id:
            return concurrent.run_id
        raise


def _first_user_message(db: Session, project_id: int) -> ProjectMessage | None:
    return db.scalar(
        select(ProjectMessage)
        .where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.sender == ProjectMessageSender.USER.value,
        )
        .order_by(ProjectMessage.sequence.asc())
        .limit(1)
    )


def _message_has_plan(db: Session, project_id: int, message_id: int) -> bool:
    return (
        db.scalar(
            select(Plan.id)
            .where(Plan.project_id == project_id, Plan.cause_message_id == message_id)
            .limit(1)
        )
        is not None
    )


def pause_active_execution(
    db: Session, user: User, project_id: int, run_id: str
) -> RequirementsStatus:
    """Fail the current running execution so the user can resume later.

    Keeps BuildRun / Plan / Task active; status becomes ``retry_available``.
    """
    lock_run(db, user, project_id, run_id)
    status = get_requirements_status(db, user, project_id)
    if status.run_id != run_id:
        raise ConflictException("构建任务不匹配")
    if status.state not in ("running", "design_running", "engineering_running", "quality_running"):
        raise ConflictException("当前没有可暂停的执行")
    if not status.task_id or not status.execution_id:
        raise ConflictException("当前没有可暂停的执行")
    paused = fail_execution(
        db,
        status.task_id,
        status.execution_id,
        error="用户已暂停，可继续处理。",
    )
    if not paused:
        raise ConflictException("执行状态已变化，请刷新后重试")
    # Persist the fence before changing the process-local marker. The worker will observe the
    # failed execution at its next checkpoint and cannot publish more progress for this attempt.
    mark_engineering_inactive(status.execution_id)
    return get_requirements_status(db, user, project_id)


def _activities_from_checkpoint(checkpoint: object) -> list[EngineeringActivity]:
    if not isinstance(checkpoint, dict):
        return []
    items: list[EngineeringActivity] = []
    for raw in checkpoint.get("activity") or []:
        if not isinstance(raw, dict):
            continue
        try:
            items.append(EngineeringActivity.model_validate(raw))
        except ValidationError:
            continue
    return items


def _activities_for_run(db: Session, run_id: str) -> list[EngineeringActivity]:
    """Rebuild the append-only visible history for every execution in one BuildRun.

    Recovery executions copy the previous checkpoint. Only their new suffix belongs to
    the new conversational turn; completed Architect/Engineer turns stay visible after
    the workflow advances to another task.
    """

    # Sort only narrow columns. Ordering rows that include TaskExecution.draft (large JSON)
    # can exhaust MySQL sort_buffer ("Out of sort memory").
    ordered = db.execute(
        select(
            Task.task_id,
            Task.title,
            Task.recipient,
            TaskExecution.execution_id,
            TaskExecution.attempt,
        )
        .join(Plan, Task.plan_id == Plan.plan_id)
        .join(TaskExecution, TaskExecution.task_id == Task.task_id)
        .where(Plan.build_run_id == run_id)
        .order_by(Plan.version.asc(), Task.position.asc(), TaskExecution.attempt.asc())
    ).all()
    if not ordered:
        return []

    execution_ids = [row.execution_id for row in ordered]
    drafts = {
        execution_id: draft
        for execution_id, draft in db.execute(
            select(TaskExecution.execution_id, TaskExecution.draft).where(
                TaskExecution.execution_id.in_(execution_ids)
            )
        ).all()
    }

    previous_by_task: dict[str, list[dict[str, object]]] = {}
    activities: list[EngineeringActivity] = []
    for task_id, title, recipient, execution_id, attempt in ordered:
        draft = drafts.get(execution_id)
        draft = draft if isinstance(draft, dict) else None
        checkpoint = draft.get("checkpoint") if draft else None
        raw_items = (
            [item for item in checkpoint.get("activity") or [] if isinstance(item, dict)]
            if isinstance(checkpoint, dict)
            else []
        )
        previous = previous_by_task.get(task_id, [])
        prefix_matches = len(raw_items) >= len(previous) and raw_items[: len(previous)] == previous
        visible_items = raw_items[len(previous) :] if prefix_matches else raw_items
        previous_by_task[task_id] = raw_items
        # Recovery attempts are continuations of the same role assignment. Exposing every
        # TaskExecution as a new chat block made one failed work item appear dozens of times.
        for index, raw in enumerate(visible_items, start=1):
            payload = dict(raw)
            raw_id = str(payload.get("id") or index)
            payload.update(
                id=f"{execution_id}:{raw_id}",
                phase_id=task_id,
                phase_label=title,
                phase_role=recipient,
                attempt=attempt,
            )
            try:
                activities.append(EngineeringActivity.model_validate(payload))
            except ValidationError:
                continue
    return activities


def _checkpoint_has_written_code(checkpoint: object) -> bool:
    if not isinstance(checkpoint, dict):
        return False
    if checkpoint.get("outcome") == "generated":
        return True
    for item in checkpoint.get("observations") or []:
        if (
            isinstance(item, dict)
            and item.get("name") in {"edit_file_by_replace", "write_new_code"}
            and item.get("ok")
        ):
            return True
    return False


def _attach_workspace_status(db: Session, result: RequirementsStatus) -> RequirementsStatus:
    if result.run_id is not None:
        result.activities = _activities_for_run(db, result.run_id)
    if result.run_id is None or result.state not in (
        "design_pending",
        "design_running",
        "engineering_pending",
        "engineering_running",
        "engineering_generated",
        "quality_pending",
        "quality_running",
        "completed",
        "quality_challenge",
        "quality_failed",
        # Budget / check pauses keep the same run workspace; still expose files.
        "retry_available",
        "stopped",
    ):
        return result
    ready = workspace_is_ready(result.project_id, result.run_id)
    result.workspace_ready = ready
    if result.state == "engineering_generated":
        result.code_ready = True
    if ready:
        result.workspace_path = str(
            default_workspace_path(settings.runtime_data_root, result.project_id, result.run_id)
        )
    return result


def get_requirements_status(db: Session, user: User, project_id: int) -> RequirementsStatus:
    """刷新页面后从 SQL 重建当前需求进度，读取不会启动执行或调用模型。"""

    project_service.get_user_project(db, user, project_id)
    result = RequirementsStatus(project_id=project_id)
    run = db.scalar(
        select(BuildRun)
        .where(BuildRun.project_id == project_id)
        .order_by(BuildRun.id.desc())
        .limit(1)
    )
    if run is None:
        return _attach_workspace_status(db, result)
    result.run_id = run.run_id
    plan = db.scalar(
        select(Plan).where(Plan.build_run_id == run.run_id).order_by(Plan.version.desc()).limit(1)
    )
    if plan is None:
        return _attach_workspace_status(db, result)
    tasks = list(db.scalars(select(Task).where(Task.plan_id == plan.plan_id)).all())
    delivery_task = tasks[0] if len(tasks) == 1 else None
    if delivery_task is not None and delivery_task.recipient == "Test Engineer":
        if (
            delivery_task.task_key,
            delivery_task.expected_output_type,
            len(delivery_task.input_configuration_item_ids),
            delivery_task.depends_on_task_ids,
        ) != (QUALITY_TASK_KEY, "test_report", 1, []):
            raise ConflictException("质量验证任务定义不符合约定")
        inputs = load_test_inputs(
            db,
            project_id,
            run.run_id,
            delivery_task.input_configuration_item_ids[0],
        )
        source = load_engineering_source(
            db,
            project_id,
            run.run_id,
            inputs.code_item.upstream_item_ids[0],
        )
        result.plan_id = plan.plan_id
        result.task_id = delivery_task.task_id
        result.message_id = source.app_spec_plan.cause_message_id
        result.app_spec = source.app_spec
        result.code_ready = True
        result.code_item_id = inputs.code_item.item_id
        result.result = ProductManagerWorkflowResult(
            project_id=project_id,
            build_run_id=run.run_id,
            cause_message_id=source.app_spec_plan.cause_message_id,
            plan_id=source.app_spec_plan.plan_id,
            task_id=source.app_spec_task.task_id,
            configuration_item_id=source.app_spec_item.item_id,
            outcome=ProductManagerWorkflowOutcome.READY_FOR_DELIVERY,
            open_questions=[],
        )
        if plan.status == "pending" and delivery_task.status == "pending":
            result.state = "quality_pending"
            return _attach_workspace_status(db, result)
        if plan.status == "running" and delivery_task.status == "running" and run.stage == "qa":
            execution = latest_execution(db, delivery_task.task_id)
            if execution is not None:
                result.execution_id = execution.execution_id
                result.execution_expires_at = execution.expires_at
                result.error = execution.error
            result.state = (
                "quality_running"
                if execution is not None
                and execution.status == "running"
                and execution.active_slot == 1
                and execution.expires_at > utc_now()
                else "retry_available"
            )
            return _attach_workspace_status(db, result)
        if plan.status == "succeeded" and delivery_task.status == "succeeded":
            saved = db.get(TaskResult, delivery_task.task_id)
            report_item = (
                db.scalar(
                    select(ConfigurationItem).where(
                        ConfigurationItem.item_id == saved.configuration_item_id
                    )
                )
                if saved
                else None
            )
            if (
                report_item is None
                or report_item.semantic_type != "test_report"
                or report_item.upstream_item_ids
                != [
                    inputs.code_item.item_id,
                    *(
                        [inputs.acceptance_test_plan_item.item_id]
                        if inputs.acceptance_test_plan_item is not None
                        else []
                    ),
                ]
            ):
                raise ConflictException("质量报告成果关联不完整")
            try:
                report = TestReport.model_validate(report_item.payload)
            except ValidationError as exc:
                raise ConflictException("质量报告正文不符合要求") from exc
            result.test_report_item_id = report_item.item_id
            result.test_challenges = report.test_challenges
            result.error = run.error
            if report.quality_conclusion == QualityConclusion.PASSED and run.status == "succeeded":
                result.state = "completed"
            elif report.quality_conclusion == QualityConclusion.BLOCKED and report.test_challenges:
                result.state = "quality_challenge"
                quality_cycles = db.scalar(
                    select(func.count())
                    .select_from(Task)
                    .join(Plan, Task.plan_id == Plan.plan_id)
                    .where(
                        Plan.project_id == project_id,
                        Plan.build_run_id == run.run_id,
                        Task.task_key == QUALITY_TASK_KEY,
                        Task.recipient == "Test Engineer",
                    )
                )
                result.retryable = int(quality_cycles or 0) < leader_service.MAX_QUALITY_CYCLES
            else:
                result.state = "quality_failed"
                if report.quality_conclusion == QualityConclusion.BLOCKED:
                    quality_cycles = db.scalar(
                        select(func.count())
                        .select_from(Task)
                        .join(Plan, Task.plan_id == Plan.plan_id)
                        .where(
                            Plan.project_id == project_id,
                            Plan.build_run_id == run.run_id,
                            Task.task_key == QUALITY_TASK_KEY,
                            Task.recipient == "Test Engineer",
                        )
                    )
                    result.retryable = (
                        not report.test_challenges
                        and int(quality_cycles or 0) < leader_service.MAX_QUALITY_CYCLES
                    )
                else:
                    result.retryable = False
            return _attach_workspace_status(db, result)
        result.state = "stopped"
        return _attach_workspace_status(db, result)
    if run.active_slot != 1:
        result.state = "stopped"
        return _attach_workspace_status(db, result)
    if run.status == "running" and run.stage not in ("pm", "architect", "developer", "qa"):
        result.state = "stopped"
        return _attach_workspace_status(db, result)
    if delivery_task is not None and delivery_task.recipient in (
        "Code Engineer",
        "Architect",
    ):
        if delivery_task.recipient == "Code Engineer":
            if (
                delivery_task.task_key,
                delivery_task.expected_output_type,
            ) != (ENGINEERING_TASK_KEY, "code"):
                raise ConflictException("工程交付任务定义不符合约定")
            if len(delivery_task.input_configuration_item_ids) not in {1, 2, 3}:
                raise ConflictException("工程交付任务缺少工程来源或包含多余输入")
        elif (
            delivery_task.task_key,
            delivery_task.expected_output_type,
        ) != (ARCHITECTURE_TASK_KEY, "system_design"):
            raise ConflictException("设计任务定义不符合约定")
        elif len(delivery_task.input_configuration_item_ids) != 1:
            raise ConflictException("设计任务没有固定唯一的需求输入")
        input_item_id = delivery_task.input_configuration_item_ids[0]
        if delivery_task.recipient == "Architect":
            source_item, source_task, source_plan, spec = load_approved_app_spec(
                db,
                project_id,
                run.run_id,
                input_item_id,
            )
            task_source_plan = source_plan
        else:
            engineering_source = load_engineering_source(
                db,
                project_id,
                run.run_id,
                input_item_id,
            )
            source_item = engineering_source.app_spec_item
            source_task = engineering_source.app_spec_task
            source_plan = engineering_source.app_spec_plan
            spec = engineering_source.app_spec
            task_source_plan = engineering_source.source_plan
        result.plan_id, result.task_id, result.message_id = (
            plan.plan_id,
            delivery_task.task_id,
            plan.cause_message_id,
        )
        result.app_spec = spec
        result.result = ProductManagerWorkflowResult(
            project_id=project_id,
            build_run_id=run.run_id,
            cause_message_id=source_plan.cause_message_id,
            plan_id=source_plan.plan_id,
            task_id=source_task.task_id,
            configuration_item_id=source_item.item_id,
            outcome=ProductManagerWorkflowOutcome.READY_FOR_DELIVERY,
            open_questions=[],
        )
        if plan.status == "pending" and delivery_task.status == "pending":
            verified: Task | None
            if delivery_task.recipient == "Code Engineer":
                extra_items = list(
                    db.scalars(
                        select(ConfigurationItem).where(
                            ConfigurationItem.item_id.in_(
                                delivery_task.input_configuration_item_ids[1:]
                            ),
                            ConfigurationItem.project_id == project_id,
                            ConfigurationItem.producer_run_id == run.run_id,
                            ConfigurationItem.state == "usable",
                        )
                    ).all()
                )
                items_by_type = {item.semantic_type: item for item in extra_items}
                report_item = items_by_type.get("test_report")
                acceptance_plan_item = items_by_type.get("acceptance_test_plan")
                if report_item is not None:
                    verified = delivery_task
                else:
                    if task_source_plan.build_run_id != run.run_id:
                        expected_inputs = [input_item_id]
                        if acceptance_plan_item is not None:
                            expected_inputs.append(acceptance_plan_item.item_id)
                        verified = (
                            delivery_task
                            if delivery_task.input_configuration_item_ids == expected_inputs
                            else None
                        )
                    else:
                        verified = find_pending_engineering_task(
                            db,
                            task_source_plan,
                            input_item_id,
                            (
                                acceptance_plan_item.item_id
                                if acceptance_plan_item is not None
                                else None
                            ),
                        )
            else:
                verified = find_architecture_task(db, task_source_plan, input_item_id)
            if verified is None or verified.task_id != delivery_task.task_id:
                raise ConflictException("工程交付任务与需求来源不一致")
            result.state = (
                "design_pending"
                if delivery_task.recipient == "Architect"
                else "engineering_pending"
            )
            return _attach_workspace_status(db, result)

        if (
            delivery_task.recipient == "Architect"
            and plan.status == "running"
            and delivery_task.status == "running"
            and run.stage == "architect"
        ):
            execution = latest_execution(db, delivery_task.task_id)
            if (
                execution is None
                or execution.status != "running"
                or execution.active_slot != 1
                or execution.expires_at <= utc_now()
            ):
                result.state = "retry_available"
            else:
                result.state = "design_running"
            if execution is not None:
                result.execution_id = execution.execution_id
                result.execution_expires_at = execution.expires_at
                result.error = execution.error
                snapshot = execution.draft if isinstance(execution.draft, dict) else None
                checkpoint = snapshot.get("checkpoint") if snapshot else None
                result.activities = _activities_from_checkpoint(checkpoint)
            return _attach_workspace_status(db, result)

        if (
            delivery_task.recipient == "Code Engineer"
            and plan.status == "running"
            and delivery_task.status == "running"
            and run.stage == "developer"
        ):
            execution = latest_execution(db, delivery_task.task_id)
            if (
                execution is None
                or execution.status != "running"
                or execution.active_slot != 1
                or execution.expires_at <= utc_now()
            ):
                result.state = "retry_available"
                if execution is not None:
                    result.execution_id = execution.execution_id
                    result.execution_expires_at = execution.expires_at
                    result.error = execution.error
                return _attach_workspace_status(db, result)
            result.state = "engineering_running"
            result.execution_id = execution.execution_id
            result.execution_expires_at = execution.expires_at
            result.error = execution.error
            snapshot = read_frozen_input_snapshot(execution)
            checkpoint = snapshot.get("checkpoint") if snapshot else None
            result.activities = _activities_from_checkpoint(checkpoint)
            if (
                isinstance(checkpoint, dict)
                and checkpoint.get("outcome") == "generated"
                and checkpoint.get("last_error")
            ):
                # Source generation and code publication are separate steps. A generated
                # checkpoint with a later publication error is recoverable, but it is not
                # yet a published code result and must keep the Continue action available.
                result.state = "retry_available"
                result.error = str(checkpoint["last_error"])
                result.code_ready = True
            elif isinstance(checkpoint, dict) and checkpoint.get("outcome") == "generated":
                result.state = "engineering_generated"
                result.code_ready = True
            elif isinstance(checkpoint, dict) and checkpoint.get("outcome") == "blocked":
                # Blocked engineering is idle until the user explicitly continues.
                result.state = "retry_available"
                result.error = str(
                    checkpoint.get("blocked_reason")
                    or checkpoint.get("last_error")
                    or result.error
                    or "构建已暂停，可继续处理。"
                )
                reason_code = str(checkpoint.get("blocked_reason_code") or "")
                result.retryable = not (
                    reason_code in {"REPEATED_CHECK_FAILURE", "REPAIR_BUDGET_EXHAUSTED"}
                    and checkpoint.get("blocked_executor_runtime_version") == EXECUTION_MODE
                )
                result.code_ready = _checkpoint_has_written_code(checkpoint)
            elif isinstance(checkpoint, dict) and checkpoint.get("blocked_reason"):
                result.error = str(checkpoint.get("blocked_reason"))
                result.code_ready = _checkpoint_has_written_code(checkpoint)
            elif isinstance(checkpoint, dict) and checkpoint.get("last_error"):
                result.error = str(checkpoint.get("last_error"))
                result.code_ready = _checkpoint_has_written_code(checkpoint)
            else:
                result.code_ready = _checkpoint_has_written_code(checkpoint)
            return _attach_workspace_status(db, result)

        if (
            delivery_task.recipient == "Code Engineer"
            and plan.status == "succeeded"
            and delivery_task.status == "succeeded"
            and run.stage == "developer"
        ):
            saved = db.get(TaskResult, delivery_task.task_id)
            code_item = (
                db.scalar(
                    select(ConfigurationItem).where(
                        ConfigurationItem.item_id == saved.configuration_item_id
                    )
                )
                if saved
                else None
            )
            if code_item is None or code_item.semantic_type != "code":
                raise ConflictException("代码成果关联不完整")
            result.state = "engineering_generated"
            result.code_ready = True
            result.code_item_id = code_item.item_id
            return _attach_workspace_status(db, result)

        result.state = "stopped"
        return _attach_workspace_status(db, result)
    if len(tasks) != 1 or tasks[0].recipient != "Product Manager":
        result.state = "stopped"
        return _attach_workspace_status(db, result)
    task = tasks[0]
    result.plan_id, result.task_id, result.message_id = (
        plan.plan_id,
        task.task_id,
        plan.cause_message_id,
    )
    if task.status == "succeeded":
        saved = db.get(TaskResult, task.task_id)
        item = (
            db.scalar(
                select(ConfigurationItem).where(
                    ConfigurationItem.item_id == saved.configuration_item_id,
                    ConfigurationItem.project_id == project_id,
                    ConfigurationItem.producer_run_id == run.run_id,
                )
            )
            if saved
            else None
        )
        if (
            item is None
            or (item.semantic_type, item.state) != ("app_spec", "usable")
            or plan.status != "succeeded"
        ):
            raise ConflictException("需求成果已不可用或关联不完整")
        spec = read_app_spec(item)
        result.app_spec = spec
        if saved is not None and saved.prompt_version == APPROVAL_VERSION:
            outcome = ProductManagerWorkflowOutcome.READY_FOR_DELIVERY
            result.state = "ready_for_delivery"
        elif spec.features:
            outcome = ProductManagerWorkflowOutcome.AWAITING_APPROVAL
            result.state = "awaiting_approval"
        else:
            outcome = ProductManagerWorkflowOutcome.NEEDS_USER_INPUT
            result.state = "needs_user_input"
        result.result = ProductManagerWorkflowResult(
            project_id=project_id,
            build_run_id=run.run_id,
            cause_message_id=plan.cause_message_id,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            configuration_item_id=item.item_id,
            outcome=outcome,
            open_questions=list(spec.open_questions),
        )
    elif task.status in ("failed", "cancelled") or plan.status in ("failed", "cancelled"):
        result.state = "stopped"
    else:
        execution = latest_execution(db, task.task_id)
        if execution is None:
            result.state = "pending"
        else:
            result.execution_id = execution.execution_id
            result.execution_expires_at = execution.expires_at
            result.error = execution.error
            result.state = (
                "running"
                if execution.status == "running" and execution.expires_at > utc_now()
                else "retry_available"
            )
    return _attach_workspace_status(db, result)
