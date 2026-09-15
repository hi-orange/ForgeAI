from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.core.exceptions import BusinessException
from app.orchestration.product_manager import run_product_manager_workflow
from app.schemas.product_manager_workflow import ProductManagerWorkflowResult
from app.schemas.project_message import ProjectMessageCreate
from app.schemas.requirements import RequirementsApproval, RequirementsExecute, RequirementsStatus
from app.schemas.response import ApiResponse, success
from app.services import engineering_run
from app.services.engineering_claim import claim_software_engineer_task
from app.services.project_manager import create_clarification_plan, create_engineering_delivery_task
from app.services.requirement_approval import approve_requirements
from app.services.requirements import get_requirements_status

router = APIRouter(prefix="/projects/{project_id}", tags=["requirements"])


def _ensure_engineering_running(
    db: DbSession,
    current_user: CurrentUser,
    project_id: int,
    run_id: str,
    *,
    force: bool = False,
) -> RequirementsStatus:
    status = get_requirements_status(db, current_user, project_id)
    if status.run_id != run_id or status.state != "engineering_running":
        return status
    if not status.execution_id or engineering_run.is_engineering_active(status.execution_id):
        return status
    if status.error and not force:
        return status
    task_id = status.result.design_task_id if status.result else None
    if not task_id:
        return status
    task, execution = claim_software_engineer_task(db, current_user, project_id, run_id, task_id)
    engineering_run.start_claimed_engineering(db, current_user, project_id, run_id, task, execution)
    return get_requirements_status(db, current_user, project_id)


@router.post(
    "/build-runs/{run_id}/requirements/{item_id}/approval",
    response_model=ApiResponse[RequirementsStatus],
)
def approve_requirement_plan(
    project_id: int,
    run_id: str,
    item_id: str,
    payload: RequirementsApproval,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    approved_id = approve_requirements(db, current_user, project_id, run_id, item_id, payload)
    delivery = create_engineering_delivery_task(db, current_user, project_id, run_id, approved_id)
    task, execution = claim_software_engineer_task(
        db, current_user, project_id, run_id, delivery.task_id
    )
    engineering_run.start_claimed_engineering(db, current_user, project_id, run_id, task, execution)
    return success(get_requirements_status(db, current_user, project_id))


@router.post(
    "/build-runs/{run_id}/engineering",
    response_model=ApiResponse[RequirementsStatus],
)
def continue_engineering(
    project_id: int,
    run_id: str,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    status = get_requirements_status(db, current_user, project_id)
    if status.run_id != run_id:
        raise BusinessException("构建任务不匹配")
    if status.state not in ("engineering_running", "design_pending"):
        raise BusinessException("当前没有可继续的工程任务")
    if status.state == "engineering_running":
        return success(
            _ensure_engineering_running(db, current_user, project_id, run_id, force=True)
        )
    task_id = status.result.design_task_id if status.result else None
    if not task_id:
        raise BusinessException("缺少工程任务")
    task, execution = claim_software_engineer_task(db, current_user, project_id, run_id, task_id)
    engineering_run.start_claimed_engineering(db, current_user, project_id, run_id, task, execution)
    return success(get_requirements_status(db, current_user, project_id))


@router.get("/requirements", response_model=ApiResponse[RequirementsStatus])
def get_requirements(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    status = get_requirements_status(db, current_user, project_id)
    if status.state == "engineering_running" and status.run_id:
        status = _ensure_engineering_running(db, current_user, project_id, status.run_id)
    return success(status)


@router.post(
    "/build-runs/{run_id}/requirements", response_model=ApiResponse[ProductManagerWorkflowResult]
)
def execute_requirements(
    project_id: int,
    run_id: str,
    payload: RequirementsExecute,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    """同步执行，FastAPI 在线程池中调用；查询接口可同时读取进度。"""
    return success(
        run_product_manager_workflow(
            db,
            current_user,
            project_id,
            run_id,
            payload.message_id,
            recovery_execution_id=payload.recovery_execution_id,
        )
    )


@router.post(
    "/build-runs/{run_id}/requirements/{item_id}/answers",
    response_model=ApiResponse[ProductManagerWorkflowResult],
)
def answer_requirements(
    project_id: int,
    run_id: str,
    item_id: str,
    payload: ProjectMessageCreate,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    plan = create_clarification_plan(db, current_user, project_id, run_id, item_id, payload)
    return success(
        run_product_manager_workflow(
            db,
            current_user,
            project_id,
            run_id,
            plan.cause_message_id,
        )
    )
