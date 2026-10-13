"""Leader coordination across role boundaries."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.models.user import User
from app.orchestration.architect import run_architecture_task
from app.services import leader as leader_service

logger = logging.getLogger("forgeai")


def start_dispatched_delivery(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task: Task,
) -> dict[str, Any]:
    """Persist one role job; only its worker owns execution claiming."""
    roles = {"Code Engineer": "engineering", "Architect": "architect"}
    if task.recipient not in roles:
        raise ValueError(f"Unsupported delivery role: {task.recipient}")
    if task.status in {"succeeded", "failed", "cancelled"}:
        return {"started": False, "task_id": task.task_id}
    job_type = roles[task.recipient]

    from app.services.workflow_jobs import enqueue_job

    job = enqueue_job(
        db,
        job_type=job_type,
        idempotency_key=f"{job_type}:{task.task_id}",
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
    on_execution_started: Callable[[TaskExecution], None] | None = None,
) -> Task:
    """Run Architect, receive its result, then let Leader assign Code Engineer."""

    design_item = run_architecture_task(
        db,
        user,
        project_id,
        run_id,
        task_id,
        recovery_execution_id=recovery_execution_id,
        on_execution_started=on_execution_started,
    )
    delivery = leader_service.dispatch_completed_design(
        db, user, project_id, run_id, design_item.item_id
    )
    start_dispatched_delivery(db, user, project_id, run_id, delivery)
    return delivery
