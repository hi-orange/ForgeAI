"""Leader coordination across role boundaries."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.task import Task
from app.models.user import User
from app.orchestration.architect import run_architecture_task
from app.services import leader as leader_service
from app.services.engineering import claim_code_engineer_task, start_claimed_engineering

logger = logging.getLogger("forgeai")


def start_dispatched_delivery(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task: Task,
) -> dict[str, Any]:
    """Start the role selected by Leader without another browser command.

    The approval request only persists the assignment. Long-running role work is
    owned by the backend process so closing or refreshing the page cannot strand
    the SOP between two roles.
    """

    if task.recipient == "Code Engineer":
        code_task, execution = claim_code_engineer_task(db, user, project_id, run_id, task.task_id)
        return start_claimed_engineering(
            db,
            user,
            project_id,
            run_id,
            code_task,
            execution,
        )
    if task.recipient != "Architect":
        raise ValueError(f"Leader 分派了不支持自动启动的角色：{task.recipient}")

    from app.services.workflow_jobs import enqueue_job

    job = enqueue_job(
        db,
        job_type="architect",
        idempotency_key=f"architect:{task.task_id}",
        project_id=project_id,
        build_run_id=run_id,
        task_id=task.task_id,
        payload={
            "user_id": user.id,
            "project_id": project_id,
            "run_id": run_id,
            "task_id": task.task_id,
        },
    )
    return {"started": True, "task_id": task.task_id, "job_id": job.job_id}


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
