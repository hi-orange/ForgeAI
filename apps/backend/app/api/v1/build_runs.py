from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.build_run import BuildRunOut
from app.schemas.response import ApiResponse, success
from app.services import build_run as build_run_service

router = APIRouter(prefix="/projects/{project_id}/build-runs", tags=["build-runs"])


@router.post("", response_model=ApiResponse[BuildRunOut], status_code=status.HTTP_202_ACCEPTED)
def create_build_run(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    """接收构建请求并返回可供轮询的 queued BuildRun。"""

    build_run = build_run_service.create_build_run(db, current_user, project_id)
    return success(BuildRunOut.model_validate(build_run), msg="构建任务已进入队列")


@router.get("/{run_id}", response_model=ApiResponse[BuildRunOut])
def get_build_run(
    project_id: int,
    run_id: str,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    """返回 BuildRun 当前状态、阶段和错误信息。"""

    build_run = build_run_service.get_user_build_run(db, current_user, project_id, run_id)
    return success(BuildRunOut.model_validate(build_run))
