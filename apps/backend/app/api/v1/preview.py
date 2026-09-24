"""Local loopback preview API for completed generated apps."""

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas.response import ApiResponse, success
from app.services import preview as preview_service
from app.services.preview import PreviewStatus

router = APIRouter(prefix="/projects/{project_id}/preview", tags=["preview"])


@router.get("", response_model=ApiResponse[PreviewStatus])
def get_preview(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    return success(preview_service.get_preview_status(db, current_user, project_id))


@router.post("/start", response_model=ApiResponse[PreviewStatus])
def start_preview(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    return success(preview_service.start_preview(db, current_user, project_id))


@router.post("/stop", response_model=ApiResponse[PreviewStatus])
def stop_preview(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    return success(preview_service.stop_preview_for_user(db, current_user, project_id))
