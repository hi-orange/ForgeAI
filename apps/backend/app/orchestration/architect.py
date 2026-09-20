"""Run one Architect task, publish its design, then hand off to Code Engineer."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.architect import generate_system_design
from app.core.settings import settings
from app.generation.workspace import default_workspace_path
from app.models.task import Task
from app.models.user import User
from app.services import architect as architect_service
from app.services import manager as manager_service
from app.services.engineering import (
    claim_code_engineer_task,
    load_approved_app_spec,
    start_claimed_engineering,
)
from app.services.task_execution import fail_execution


def run_architecture_and_start_engineering(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    recovery_execution_id: str | None = None,
) -> Task:
    """Execute the exact design assignment and start its downstream coding task."""

    task, execution = architect_service.claim_architect_task(
        db,
        user,
        project_id,
        run_id,
        task_id,
        recovery_execution_id=recovery_execution_id,
    )
    with Session(bind=db.get_bind(), autoflush=False) as reader:
        app_spec_item_id = task.input_configuration_item_ids[0]
        _, _, _, spec = load_approved_app_spec(reader, project_id, run_id, app_spec_item_id)
    workspace_root = default_workspace_path(settings.runtime_data_root, project_id, run_id)
    try:
        design = generate_system_design(spec=spec, workspace_root=workspace_root)
        design_item = architect_service.complete_architect_task(
            db,
            user,
            project_id,
            run_id,
            task_id,
            execution.execution_id,
            design,
        )
    except Exception:
        fail_execution(
            db,
            task_id,
            execution.execution_id,
            error="Architect 执行失败，可重试恢复。",
        )
        raise

    delivery = manager_service.create_engineering_delivery_task(
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
