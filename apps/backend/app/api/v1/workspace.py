from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.schemas.response import ApiResponse, success
from app.services import workspace_files as workspace_files_service
from app.services.workspace_files import WorkspaceFileContent, WorkspaceListing

router = APIRouter(prefix="/projects/{project_id}/workspace", tags=["workspace"])


@router.get("", response_model=ApiResponse[WorkspaceListing])
def get_workspace(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    """List text and image files in the materialized engineering workspace."""
    return success(workspace_files_service.list_project_workspace(db, current_user, project_id))


@router.get("/file", response_model=ApiResponse[WorkspaceFileContent])
def get_workspace_file(
    project_id: int,
    db: DbSession,
    current_user: CurrentUser,
    path: str = Query(min_length=1, max_length=512),
) -> dict:
    """Read one workspace file (UTF-8 text or base64 image; path-safe, size-capped)."""
    return success(
        workspace_files_service.read_project_workspace_file(db, current_user, project_id, path)
    )
