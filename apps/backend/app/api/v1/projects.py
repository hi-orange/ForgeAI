from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas.project import (
    ProjectApproveSpec,
    ProjectCreate,
    ProjectElementAiEdit,
    ProjectElementAiReply,
    ProjectOut,
    ProjectStart,
    ProjectStartOut,
    ProjectWebsiteEdit,
    ProjectWebsiteRevise,
    ProjectWebsiteReviseReply,
)
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


@router.post("/{project_id}/approve-spec", response_model=ApiResponse[ProjectOut])
def approve_project_spec(
    project_id: int,
    payload: ProjectApproveSpec,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    project = project_service.approve_project_spec(db, current_user, project_id, payload)
    return success(ProjectOut.model_validate(project), msg="网站规格已批准")


@router.post("/{project_id}/build", response_model=ApiResponse[ProjectOut])
def build_project(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    project = project_service.build_project(db, current_user, project_id)
    return success(ProjectOut.model_validate(project), msg="网站构建完成")


@router.patch("/{project_id}/website", response_model=ApiResponse[ProjectOut])
def edit_project_website(
    project_id: int,
    payload: ProjectWebsiteEdit,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    project = project_service.edit_project_website(db, current_user, project_id, payload)
    return success(ProjectOut.model_validate(project), msg="网站修改已保存")


@router.post(
    "/{project_id}/website/element-suggestion",
    response_model=ApiResponse[ProjectElementAiReply],
)
def suggest_project_element_edit(
    project_id: int,
    payload: ProjectElementAiEdit,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    reply = project_service.suggest_project_element_edit(db, current_user, project_id, payload)
    msg = "已保存网站修改" if reply.mode == "applied" else "网站修改助手已回复"
    return success(reply, msg=msg)


@router.post(
    "/{project_id}/website/revise",
    response_model=ApiResponse[ProjectWebsiteReviseReply],
)
def revise_project_website(
    project_id: int,
    payload: ProjectWebsiteRevise,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    reply = project_service.revise_project_website(db, current_user, project_id, payload)
    msg = "已保存网站修改" if reply.mode == "applied" else "网站修改助手已回复"
    return success(reply, msg=msg)
