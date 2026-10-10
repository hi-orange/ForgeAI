from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas.evaluation import (
    AgentDecisionEventOut,
    AgentEvalLabelCreate,
    AgentEvaluationReport,
)
from app.schemas.response import ApiResponse, success
from app.services import decision_event as decision_event_service
from app.services import evaluation as evaluation_service
from app.services import project as project_service

router = APIRouter(prefix="/projects/{project_id}", tags=["agent-evaluation"])


@router.get("/agent-eval", response_model=ApiResponse[AgentEvaluationReport])
def agent_eval(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    return success(evaluation_service.evaluate_project(db, current_user, project_id))


@router.get("/agent-events", response_model=ApiResponse[list[AgentDecisionEventOut]])
def agent_events(project_id: int, db: DbSession, current_user: CurrentUser) -> dict:
    project_service.get_user_project(db, current_user, project_id)
    rows = decision_event_service.list_project_decisions(db, project_id)
    return success([AgentDecisionEventOut.model_validate(row) for row in rows])


@router.post("/agent-eval/labels", response_model=ApiResponse[AgentEvaluationReport])
def label_agent_case(
    project_id: int,
    payload: AgentEvalLabelCreate,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    evaluation_service.save_eval_label(db, current_user, project_id, payload)
    return success(evaluation_service.evaluate_project(db, current_user, project_id))
