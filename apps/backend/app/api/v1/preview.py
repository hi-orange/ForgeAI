"""Local loopback preview API for completed generated apps."""

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas.response import ApiResponse, success
from app.services import preview as preview_service
from app.services import preview_design
from app.services.preview import PreviewStatus
from app.services.preview_design import PreviewDesignState, PreviewDesignUpdate

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


@router.get("/design", response_model=ApiResponse[PreviewDesignState])
def get_preview_design(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    return success(preview_design.get_design_state(db, current_user, project_id))


@router.put("/design", response_model=ApiResponse[PreviewDesignState])
def save_preview_design(
    project_id: int,
    payload: PreviewDesignUpdate,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    return success(preview_design.save_design_state(db, current_user, project_id, payload))
