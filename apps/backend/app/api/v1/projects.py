from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas.project import ProjectCreate, ProjectOut, ProjectStart, ProjectStartOut
from app.schemas.response import ApiResponse, success
from app.services import project as project_service

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ApiResponse[ProjectOut])
def create_project(
    payload: ProjectCreate,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    project = project_service.create_project(db, current_user, payload)
    return success(ProjectOut.model_validate(project))


@router.get("", response_model=ApiResponse[list[ProjectOut]])
def list_projects(db: DbSession, current_user: CurrentUser) -> dict:
    projects = project_service.list_user_projects(db, current_user)
    return success([ProjectOut.model_validate(p) for p in projects])


@router.get("/{project_id}", response_model=ApiResponse[ProjectOut])
def get_project(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    project = project_service.get_user_project(db, current_user, project_id)
    return success(ProjectOut.model_validate(project))


@router.post("/{project_id}/start", response_model=ApiResponse[ProjectStartOut])
def start_project(
    project_id: int,
    db: DbSession,
    current_user: CurrentUser,
    payload: ProjectStart | None = None,
) -> dict:
    project, workflow_id = project_service.start_project(
        db,
        current_user,
        project_id,
        payload,
    )
    return success(
        ProjectStartOut(
            project=ProjectOut.model_validate(project),
            workflow_id=workflow_id,
        )
    )
