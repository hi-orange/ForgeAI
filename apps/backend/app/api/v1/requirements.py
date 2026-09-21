from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.orchestration.product_manager import run_product_manager_workflow
from app.schemas.product_manager_workflow import ProductManagerWorkflowResult
from app.schemas.project_message import ProjectMessageCreate
from app.schemas.requirements import (
    RequirementsApproval,
    RequirementsExecute,
    RequirementsStart,
    RequirementsStatus,
    RequirementsSubmit,
)
from app.schemas.response import ApiResponse, success
from app.services.leader import create_clarification_plan
from app.services.requirements import (
    approve_requirement_plan,
    continue_engineering,
    continue_requirements,
    ensure_engineering_running,
    get_requirements_status,
    pause_active_execution,
    start_requirements,
    submit_requirements,
)

router = APIRouter(prefix="/projects/{project_id}", tags=["requirements"])


@router.post("/requirements/submit", response_model=ApiResponse[RequirementsStatus])
def submit_requirement_turn(
    project_id: int,
    payload: RequirementsSubmit,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    """Create or answer a message and advance the build when classification allows it."""
    return success(
        submit_requirements(
            db,
            current_user,
            project_id,
            payload.content,
            payload.client_message_id,
        )
    )


@router.post("/requirements/start", response_model=ApiResponse[RequirementsStatus])
def start_requirement_build(
    project_id: int,
    db: DbSession,
    current_user: CurrentUser,
    payload: RequirementsStart | None = None,
) -> dict:
    """Kick off planning from an existing user message (e.g. home prompt on mount)."""
    message_id = payload.message_id if payload is not None else None
    return success(start_requirements(db, current_user, project_id, message_id))


@router.post("/requirements/continue", response_model=ApiResponse[RequirementsStatus])
def continue_requirement_build(
    project_id: int,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    """Resume PM recovery or engineering from the project's current status."""
    return success(continue_requirements(db, current_user, project_id))


@router.post(
    "/build-runs/{run_id}/pause",
    response_model=ApiResponse[RequirementsStatus],
)
def pause_build_run(
    project_id: int,
    run_id: str,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    return success(pause_active_execution(db, current_user, project_id, run_id))


@router.post(
    "/build-runs/{run_id}/requirements/{item_id}/approval",
    response_model=ApiResponse[RequirementsStatus],
)
def approve_requirement_plan_route(
    project_id: int,
    run_id: str,
    item_id: str,
    payload: RequirementsApproval,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    return success(approve_requirement_plan(db, current_user, project_id, run_id, item_id, payload))


@router.post(
    "/build-runs/{run_id}/engineering",
    response_model=ApiResponse[RequirementsStatus],
)
def continue_engineering_route(
    project_id: int,
    run_id: str,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    return success(continue_engineering(db, current_user, project_id, run_id))


@router.get("/requirements", response_model=ApiResponse[RequirementsStatus])
def get_requirements(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    status = get_requirements_status(db, current_user, project_id)
    if status.state == "engineering_running" and status.run_id:
        status = ensure_engineering_running(db, current_user, project_id, status.run_id)
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
