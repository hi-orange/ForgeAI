from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.task import Task
from app.models.task_result import TaskResult
from app.models.user import User
from app.schemas.product_manager_workflow import (
    ProductManagerWorkflowOutcome,
    ProductManagerWorkflowResult,
)
from app.schemas.requirements import RequirementsStatus
from app.services import project as project_service
from app.services.design_handoff import (
    APPROVAL_VERSION,
    ENGINEERING_TASK_KEY,
    LEGACY_DESIGN_TASK_KEY,
    find_pending_engineering_task,
    load_approved_app_spec,
)
from app.services.requirement_inputs import read_app_spec
from app.services.task_execution import latest_execution, utc_now


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
        return result
    result.run_id = run.run_id
    plan = db.scalar(
        select(Plan).where(Plan.build_run_id == run.run_id).order_by(Plan.version.desc()).limit(1)
    )
    if run.active_slot != 1 or (run.status == "running" and run.stage != "pm"):
        result.state = "stopped"
        return result
    if plan is None:
        return result
    tasks = list(db.scalars(select(Task).where(Task.plan_id == plan.plan_id)).all())
    delivery_task = tasks[0] if len(tasks) == 1 else None
    if delivery_task is not None and delivery_task.recipient in (
        "SoftwareEngineer",
        "SolutionArchitect",
    ):
        if plan.status != "pending" or delivery_task.status != "pending":
            result.state = "stopped"
            return result
        if len(delivery_task.input_configuration_item_ids) != 1:
            raise ConflictException("工程交付任务没有固定唯一的需求输入")
        if delivery_task.recipient == "SoftwareEngineer":
            if (
                delivery_task.task_key,
                delivery_task.expected_output_type,
            ) != (ENGINEERING_TASK_KEY, "code"):
                raise ConflictException("工程交付任务定义不符合约定")
        elif (
            delivery_task.task_key,
            delivery_task.expected_output_type,
        ) != (LEGACY_DESIGN_TASK_KEY, "system_design"):
            raise ConflictException("设计任务定义不符合约定")
        source_item, source_task, source_plan, spec = load_approved_app_spec(
            db,
            project_id,
            run.run_id,
            delivery_task.input_configuration_item_ids[0],
        )
        verified = find_pending_engineering_task(db, source_plan, source_item.item_id)
        if verified is None or verified.task_id != delivery_task.task_id:
            raise ConflictException("工程交付任务与需求来源不一致")
        result.state = "design_pending"
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
        source_result = db.get(TaskResult, source_task.task_id)
        if (
            delivery_task.task_key == LEGACY_DESIGN_TASK_KEY
            and source_result is not None
            and source_result.prompt_version != APPROVAL_VERSION
        ):
            # A pre-approval legacy assignment is historical work, not user consent.
            result.state = "awaiting_approval"
            result.plan_id, result.task_id = source_plan.plan_id, source_task.task_id
            result.result.outcome = ProductManagerWorkflowOutcome.AWAITING_APPROVAL
            result.result.design_plan_id = result.result.design_task_id = None
        return result
    if len(tasks) != 1 or tasks[0].recipient != "ProductManager":
        result.state = "stopped"
        return result
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
    return result
