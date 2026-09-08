from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.orchestration.product_manager import run_product_manager_workflow
from app.schemas.product_manager_workflow import ProductManagerWorkflowResult
from app.schemas.project_message import ProjectMessageCreate
from app.schemas.requirements import RequirementsExecute, RequirementsStatus
from app.schemas.response import ApiResponse, success
from app.services.project_manager import create_clarification_plan
from app.services.requirements import get_requirements_status

router = APIRouter(prefix="/projects/{project_id}", tags=["requirements"])


@router.get("/requirements", response_model=ApiResponse[RequirementsStatus])
def get_requirements(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    return success(get_requirements_status(db, current_user, project_id))


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
