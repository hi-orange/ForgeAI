"""Leader coordination across role boundaries."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.task import Task
from app.models.user import User
from app.orchestration.architect import run_architecture_task
from app.services import leader as leader_service
from app.services.engineering import claim_code_engineer_task, start_claimed_engineering


def run_architect_and_continue(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    recovery_execution_id: str | None = None,
) -> Task:
    """Run Architect, receive its result, then let Leader assign Code Engineer."""

    design_item = run_architecture_task(
        db,
        user,
        project_id,
        run_id,
        task_id,
        recovery_execution_id=recovery_execution_id,
    )
    delivery = leader_service.dispatch_completed_design(
        db, user, project_id, run_id, design_item.item_id
    )
    code_task, code_execution = claim_code_engineer_task(
        db, user, project_id, run_id, delivery.task_id
    )
    start_claimed_engineering(
        db,
        user,
        project_id,
        run_id,
        code_task,
        code_execution,
    )
    return code_task
