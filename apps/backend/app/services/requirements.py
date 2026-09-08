from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.task import Task
from app.models.task_result import TaskResult
from app.models.user import User
from app.schemas.app_spec import APP_SPEC_SCHEMA_VERSION, AppSpec
from app.schemas.product_manager_workflow import (
    ProductManagerWorkflowOutcome,
    ProductManagerWorkflowResult,
)
from app.schemas.requirements import RequirementsStatus
from app.services import project as project_service
from app.services.design_handoff import find_pending_design_task, load_design_source
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
    if len(tasks) == 1 and tasks[0].recipient == "SolutionArchitect":
        design_task = tasks[0]
        if plan.status != "pending" or design_task.status != "pending":
            result.state = "stopped"
            return result
        if len(design_task.input_configuration_item_ids) != 1:
            raise ConflictException("设计任务没有固定唯一的需求输入")
        source_item, source_task, source_plan, spec = load_design_source(
            db,
            project_id,
            run.run_id,
            design_task.input_configuration_item_ids[0],
        )
        verified = find_pending_design_task(db, source_plan, source_item.item_id)
        if verified is None or verified.task_id != design_task.task_id:
            raise ConflictException("设计任务与需求来源不一致")
        result.state = "design_pending"
        result.plan_id, result.task_id, result.message_id = (
            plan.plan_id,
            design_task.task_id,
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
            design_task_id=design_task.task_id,
        )
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
            or (item.semantic_type, item.state, item.schema_version)
            != ("app_spec", "usable", APP_SPEC_SCHEMA_VERSION)
            or plan.status != "succeeded"
        ):
            raise ConflictException("需求成果已不可用或关联不完整")
        try:
            spec = AppSpec.model_validate(item.payload)
        except ValidationError as exc:
            raise ConflictException("需求正文不符合要求") from exc
        result.app_spec = spec
        outcome = (
            ProductManagerWorkflowOutcome.NEEDS_USER_INPUT
            if spec.open_questions
            else ProductManagerWorkflowOutcome.READY_FOR_DESIGN
        )
        result.state = "needs_user_input" if spec.open_questions else "ready_for_design"
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
