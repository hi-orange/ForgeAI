from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.core.exceptions import NotFoundException
from app.schemas.response import ApiResponse, success
from app.schemas.security import SecurityReportOut
from app.services import project as project_service
from app.services.security_gate import latest_security_report

router = APIRouter(prefix="/projects/{project_id}/security", tags=["security"])


@router.get("/latest", response_model=ApiResponse[SecurityReportOut])
def get_latest_security_report(
    project_id: int,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    project_service.get_user_project(db, current_user, project_id)
    report = latest_security_report(db, project_id)
    if report is None:
        raise NotFoundException("尚无独立安全报告")
    return success(SecurityReportOut.model_validate(report))
