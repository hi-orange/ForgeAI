"""Run one assigned Test Engineer task against an exact code identity."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.test_engineer import verify_code
from app.core.settings import settings
from app.generation.workspace import default_workspace_path
from app.models.configuration_item import ConfigurationItem
from app.models.user import User
from app.services import test_engineer as test_engineer_service
from app.services.task_execution import fail_execution


def run_test_engineer_task(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    recovery_execution_id: str | None = None,
) -> ConfigurationItem:
    """Verify code read-only, then publish the immutable quality report."""

    task, execution = test_engineer_service.claim_test_engineer_task(
        db,
        user,
        project_id,
        run_id,
        task_id,
        recovery_execution_id=recovery_execution_id,
    )
    with Session(bind=db.get_bind(), autoflush=False) as reader:
        inputs = test_engineer_service.load_test_inputs(
            reader,
            project_id,
            run_id,
            task.input_configuration_item_ids[0],
        )
    workspace_root = default_workspace_path(settings.runtime_data_root, project_id, run_id)
    try:
        report = verify_code(
            spec=inputs.app_spec,
            system_design=inputs.system_design,
            code_item_id=inputs.code_item.item_id,
            code_source_hash=inputs.code_artifact.source_hash,
            workspace_root=workspace_root,
        )
        return test_engineer_service.complete_test_engineer_task(
            db,
            user,
            project_id,
            run_id,
            task_id,
            execution.execution_id,
            report,
        )
    except Exception:
        fail_execution(
            db,
            task_id,
            execution.execution_id,
            error="Test Engineer 执行失败，可重试恢复。",
        )
        raise
