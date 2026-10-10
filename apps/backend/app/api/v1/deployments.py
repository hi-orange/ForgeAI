from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.deployment import DeploymentCreate, DeploymentOut
from app.schemas.response import ApiResponse, success
from app.services import deployment as deployment_service

router = APIRouter(prefix="/projects/{project_id}/deployments", tags=["deployments"])


@router.post("", response_model=ApiResponse[DeploymentOut], status_code=status.HTTP_202_ACCEPTED)
def create_deployment(
    project_id: int,
    payload: DeploymentCreate,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    del payload  # provider is currently fixed and validated by the request schema.
    deployment = deployment_service.start_deployment(db, current_user, project_id)
    return success(DeploymentOut.model_validate(deployment), msg="发布任务已启动")


@router.get("", response_model=ApiResponse[list[DeploymentOut]])
def deployments(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    rows = deployment_service.list_deployments(db, current_user, project_id)
    return success([DeploymentOut.model_validate(row) for row in rows])


@router.get("/{deployment_id}", response_model=ApiResponse[DeploymentOut])
def deployment(
    project_id: int, deployment_id: str, db: DbSession, current_user: CurrentUser
) -> dict:
    row = deployment_service.get_deployment(db, current_user, project_id, deployment_id)
    return success(DeploymentOut.model_validate(row))


@router.post("/{deployment_id}/rollback", response_model=ApiResponse[DeploymentOut])
def rollback(project_id: int, deployment_id: str, db: DbSession, current_user: CurrentUser) -> dict:
    row = deployment_service.rollback_deployment(db, current_user, project_id, deployment_id)
    return success(DeploymentOut.model_validate(row), msg="已回滚到上一发布版本")
