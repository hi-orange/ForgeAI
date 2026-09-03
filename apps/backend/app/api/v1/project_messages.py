from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.schemas.project_message import ProjectMessageCreate, ProjectMessageOut
from app.schemas.project_message_classification import ProjectMessageClassificationOut
from app.schemas.response import ApiResponse, success
from app.services import project_manager as project_manager_service
from app.services import project_message as project_message_service

router = APIRouter(prefix="/projects/{project_id}/messages", tags=["project-messages"])


@router.post("", response_model=ApiResponse[ProjectMessageOut])
def create_project_message(
    project_id: int,
    payload: ProjectMessageCreate,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    message = project_message_service.create_user_project_message(
        db,
        current_user,
        project_id,
        payload,
    )
    return success(ProjectMessageOut.model_validate(message))


@router.get("", response_model=ApiResponse[list[ProjectMessageOut]])
def list_project_messages(
    project_id: int,
    db: DbSession,
    current_user: CurrentUser,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict:
    messages = project_message_service.list_user_project_messages(
        db,
        current_user,
        project_id,
        after_sequence=after_sequence,
        limit=limit,
    )
    return success([ProjectMessageOut.model_validate(message) for message in messages])


@router.post(
    "/{message_id}/classification",
    response_model=ApiResponse[ProjectMessageClassificationOut],
)
def classify_project_message(
    project_id: int,
    message_id: int,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    classification = project_manager_service.classify_user_message(
        db,
        current_user,
        project_id,
        message_id,
    )
    return success(ProjectMessageClassificationOut.model_validate(classification))


@router.get(
    "/{message_id}/classification",
    response_model=ApiResponse[ProjectMessageClassificationOut],
)
def get_project_message_classification(
    project_id: int,
    message_id: int,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    classification = project_manager_service.get_user_message_classification(
        db,
        current_user,
        project_id,
        message_id,
    )
    return success(ProjectMessageClassificationOut.model_validate(classification))
