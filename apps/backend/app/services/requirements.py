from pydantic import ValidationError
from sqlalchemy import select
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
from app.models.task_result import TaskResult
from app.models.user import User
from app.orchestration.leader import run_architect_and_continue
from app.orchestration.product_manager import run_product_manager_workflow
from app.schemas.product_manager_workflow import (
    ProductManagerWorkflowOutcome,
    ProductManagerWorkflowResult,
)
from app.schemas.project_message import ProjectMessageCreate
from app.schemas.requirements import EngineeringActivity, RequirementsApproval, RequirementsStatus
from app.services import build_run as build_run_service
from app.services import engineering
from app.services import leader as leader_service
from app.services import project as project_service
from app.services import project_message as project_message_service
from app.services import project_message_classification as classification_service
from app.services import task as task_service
from app.services.app_spec import approve_requirements as persist_approved_requirements
from app.services.app_spec import read_app_spec
from app.services.engineering import (
    APPROVAL_VERSION,
    ARCHITECTURE_TASK_KEY,
    ENGINEERING_TASK_KEY,
    find_architecture_task,
    find_pending_engineering_task,
    load_approved_app_spec,
    load_engineering_source,
    mark_engineering_inactive,
    read_frozen_input_snapshot,
)
from app.services.leader import create_clarification_plan
from app.services.task_execution import fail_execution, latest_execution, lock_run, utc_now

WRITABLE_STATES = frozenset({"not_started", "needs_user_input", "awaiting_approval"})
CONTINUE_PM_STATES = frozenset({"pending", "retry_available", "ready_for_design"})


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
    """Classify one persisted user turn and start planning only for product work."""

    classification = classification_service.classify_user_message(db, user, project_id, message_id)
    if classification.category != ProjectMessageCategory.PRODUCT_CHANGE.value:
        project_message_service.create_assistant_project_message(
            db,
            user,
            project_id,
            _guidance_for_category(classification.category),
            client_message_id=f"guidance:msg:{message_id}:{classification.category}",
        )
        return get_requirements_status(db, user, project_id)

    status = get_requirements_status(db, user, project_id)
    run_id = _ensure_run_id(db, user, project_id, status)
    run_product_manager_workflow(db, user, project_id, run_id, message_id)
    return get_requirements_status(db, user, project_id)


def continue_requirements(db: Session, user: User, project_id: int) -> RequirementsStatus:
    """Resume whichever saved PM or engineering step currently owns the BuildRun."""

    status = get_requirements_status(db, user, project_id)
    if not status.run_id:
        raise BusinessException("当前没有可继续的构建")

    if (
        status.state == "engineering_running"
        or status.state == "design_pending"
        or (status.state == "retry_available" and status.result and status.result.design_task_id)
    ):
        return continue_engineering(db, user, project_id, status.run_id)

    if status.state == "ready_for_design" and status.result:
        leader_service.dispatch_approved_requirements(
            db,
            user,
            project_id,
            status.run_id,
            status.result.configuration_item_id,
        )
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
    leader_service.dispatch_approved_requirements(db, user, project_id, run_id, approved_id)
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
        if status.state == "design_pending":
            task, execution = engineering.claim_code_engineer_task(
                db, user, project_id, run_id, current_task.task_id
            )
            engineering.start_claimed_engineering(db, user, project_id, run_id, task, execution)
            return get_requirements_status(db, user, project_id)
        if status.state not in ("engineering_running", "retry_available"):
            raise BusinessException("当前没有可继续的 Code Engineer 任务")
    if status.state == "retry_available" and status.result and status.result.design_task_id:
        return _resume_engineering_from_retry(db, user, project_id, run_id, status)
    if status.state != "engineering_running":
        raise BusinessException("当前没有可继续的工程任务")
    if status.state == "engineering_running":
        return ensure_engineering_running(db, user, project_id, run_id, force=True)
    task_id = status.result.design_task_id if status.result else None
    if not task_id:
        raise BusinessException("缺少工程任务")
    task, execution = engineering.claim_code_engineer_task(db, user, project_id, run_id, task_id)
    engineering.start_claimed_engineering(db, user, project_id, run_id, task, execution)
    return get_requirements_status(db, user, project_id)


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
    task_id = status.result.design_task_id if status.result else None
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
    task_id = status.result.design_task_id if status.result else None
    if not task_id or not status.execution_id:
        raise BusinessException("当前没有可继续的工程任务")
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


def _guidance_for_category(category: str) -> str:
    if category == ProjectMessageCategory.INQUIRY.value:
        return "先具体描述一下你想做的应用或功能，我再帮你整理构建计划。"
    if category == ProjectMessageCategory.STOP.value:
        return "当前还没有开始构建。描述你想做的应用后，就可以开始了。"
    if category == ProjectMessageCategory.IMPLEMENTATION_REPAIR.value:
        return "现在还没有可修复的实现。先描述你想做的应用，我会整理一份构建计划。"
    return "请描述你要做的应用或功能，我再开始整理需求。"


def _ensure_run_id(db: Session, user: User, project_id: int, status: RequirementsStatus) -> str:
    if status.run_id:
        return status.run_id
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
    if status.state not in ("running", "design_running", "engineering_running"):
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


def _attach_workspace_status(result: RequirementsStatus) -> RequirementsStatus:
    if result.run_id is None or result.state not in (
        "design_pending",
        "design_running",
        "engineering_running",
        "engineering_generated",
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
        return _attach_workspace_status(result)
    result.run_id = run.run_id
    plan = db.scalar(
        select(Plan).where(Plan.build_run_id == run.run_id).order_by(Plan.version.desc()).limit(1)
    )
    if run.active_slot != 1:
        result.state = "stopped"
        return _attach_workspace_status(result)
    if run.status == "running" and run.stage not in ("pm", "architect", "developer"):
        result.state = "stopped"
        return _attach_workspace_status(result)
    if plan is None:
        return _attach_workspace_status(result)
    tasks = list(db.scalars(select(Task).where(Task.plan_id == plan.plan_id)).all())
    delivery_task = tasks[0] if len(tasks) == 1 else None
    if delivery_task is not None and delivery_task.recipient in (
        "Code Engineer",
        "Architect",
    ):
        if len(delivery_task.input_configuration_item_ids) != 1:
            raise ConflictException("工程交付任务没有固定唯一的需求输入")
        if delivery_task.recipient == "Code Engineer":
            if (
                delivery_task.task_key,
                delivery_task.expected_output_type,
            ) != (ENGINEERING_TASK_KEY, "code"):
                raise ConflictException("工程交付任务定义不符合约定")
        elif (
            delivery_task.task_key,
            delivery_task.expected_output_type,
        ) != (ARCHITECTURE_TASK_KEY, "system_design"):
            raise ConflictException("设计任务定义不符合约定")
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
            source_plan.cause_message_id,
        )
        result.app_spec = spec
        result.result = ProductManagerWorkflowResult(
            project_id=project_id,
            build_run_id=run.run_id,
            cause_message_id=source_plan.cause_message_id,
            plan_id=source_plan.plan_id,
            task_id=source_task.task_id,
            configuration_item_id=source_item.item_id,
            outcome=ProductManagerWorkflowOutcome.READY_FOR_DESIGN,
            open_questions=[],
            design_plan_id=plan.plan_id,
            design_task_id=delivery_task.task_id,
        )
        if plan.status == "pending" and delivery_task.status == "pending":
            verified = (
                find_architecture_task(db, task_source_plan, input_item_id)
                if delivery_task.recipient == "Architect"
                else find_pending_engineering_task(db, task_source_plan, input_item_id)
            )
            if verified is None or verified.task_id != delivery_task.task_id:
                raise ConflictException("工程交付任务与需求来源不一致")
            result.state = "design_pending"
            return _attach_workspace_status(result)

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
            return _attach_workspace_status(result)

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
                return _attach_workspace_status(result)
            result.state = "engineering_running"
            result.execution_id = execution.execution_id
            result.execution_expires_at = execution.expires_at
            result.error = execution.error
            snapshot = read_frozen_input_snapshot(execution)
            checkpoint = snapshot.get("checkpoint") if snapshot else None
            result.activities = _activities_from_checkpoint(checkpoint)
            if isinstance(checkpoint, dict) and checkpoint.get("outcome") == "generated":
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
                result.code_ready = _checkpoint_has_written_code(checkpoint)
            elif isinstance(checkpoint, dict) and checkpoint.get("blocked_reason"):
                result.error = str(checkpoint.get("blocked_reason"))
                result.code_ready = _checkpoint_has_written_code(checkpoint)
            elif isinstance(checkpoint, dict) and checkpoint.get("last_error"):
                result.error = str(checkpoint.get("last_error"))
                result.code_ready = _checkpoint_has_written_code(checkpoint)
            else:
                result.code_ready = _checkpoint_has_written_code(checkpoint)
            return _attach_workspace_status(result)

        result.state = "stopped"
        return _attach_workspace_status(result)
    if len(tasks) != 1 or tasks[0].recipient != "Product Manager":
        result.state = "stopped"
        return _attach_workspace_status(result)
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
            outcome = ProductManagerWorkflowOutcome.READY_FOR_DESIGN
            result.state = "ready_for_design"
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
    return _attach_workspace_status(result)
