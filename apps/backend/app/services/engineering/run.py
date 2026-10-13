"""Start the engineering coding loop after a Code Engineer task is claimed."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.models.user import User
from app.services.engineering.claim import read_frozen_input_snapshot
from app.services.engineering.completion import complete_code_engineer_task
from app.services.task_execution import latest_execution, save_execution_draft, utc_now

logger = logging.getLogger("forgeai")


def is_engineering_active(db: Session, execution_id: str) -> bool:
    """Check durable ownership in the caller transaction."""

    from sqlalchemy import select

    from app.models.workflow_job import WorkflowJob

    return (
        db.scalar(
            select(WorkflowJob.job_id).where(
                WorkflowJob.execution_id == execution_id,
                WorkflowJob.status.in_(("queued", "running", "retry_wait")),
            )
        )
        is not None
    )


def _record_loop_failure(
    session_factory: Any,
    task_id: str,
    execution_id: str,
    message: str,
) -> None:
    try:
        with session_factory() as db:
            execution = latest_execution(db, task_id, lock=True)
            if execution is None or execution.execution_id != execution_id:
                return
            if execution.status != "running" or execution.active_slot != 1:
                return
            if execution.expires_at <= utc_now():
                return
            snapshot = read_frozen_input_snapshot(execution)
            if snapshot is None:
                return
            checkpoint = snapshot.get("checkpoint")
            if not isinstance(checkpoint, dict):
                checkpoint = {
                    "kind": "engineering_checkpoint",
                    "schema_version": 1,
                    "work_items": [],
                    "activity": [],
                    "outcome": "running",
                }
            checkpoint["last_error"] = message[:500]
            activity = list(checkpoint.get("activity") or [])
            activity.append(
                {
                    "id": f"error_{len(activity) + 1}",
                    "name": "error",
                    "label": "Blocked",
                    "detail": message[:200],
                    "ok": False,
                }
            )
            checkpoint["activity"] = activity
            snapshot["checkpoint"] = checkpoint
            save_execution_draft(db, execution, snapshot)
    except Exception:
        logger.exception("failed to persist engineering loop error execution_id=%s", execution_id)


def dispatch_code_validation(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    code_item_id: str,
    *,
    enqueue_quality: bool = True,
) -> Task:
    """Use the same durable handoff for publication and worker recovery."""
    # Import locally: Leader depends on the engineering service facade.
    from app.services import leader as leader_service

    quality_task = leader_service.dispatch_completed_code(
        db,
        user,
        project_id,
        run_id,
        code_item_id,
    )
    if enqueue_quality:
        from app.services.security_gate import enqueue_security_gate
        from app.services.workflow_jobs import enqueue_job

        if quality_task.status in {"pending", "running"}:
            enqueue_job(
                db,
                job_type="quality",
                idempotency_key=f"quality:{quality_task.task_id}",
                project_id=project_id,
                build_run_id=run_id,
                task_id=quality_task.task_id,
                payload={
                    "user_id": user.id,
                    "project_id": project_id,
                    "run_id": run_id,
                    "task_id": quality_task.task_id,
                },
            )
        enqueue_security_gate(
            db,
            user_id=user.id,
            project_id=project_id,
            run_id=run_id,
            code_item_id=code_item_id,
        )
    return quality_task


def _run_and_publish(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    execution_id: str,
    *,
    session_factory: Any | None = None,
    enqueue_quality: bool = True,
) -> dict:
    # Lazy import: orchestration.code_engineer imports engineering.claim, and
    # engineering.__init__ imports this module — avoid a circular import at load time.
    from app.orchestration.code_engineer import run_engineering_workflow

    result = dict(
        run_engineering_workflow(
            db,
            user,
            project_id,
            run_id,
            task_id,
            execution_id,
            session_factory=session_factory,
        )
    )
    if result.get("outcome") != "generated":
        return result
    code_item = complete_code_engineer_task(
        db,
        user,
        project_id,
        run_id,
        task_id,
        execution_id,
    )
    quality_task = dispatch_code_validation(
        db, user, project_id, run_id, code_item.item_id, enqueue_quality=enqueue_quality
    )
    result.update(code_item_id=code_item.item_id, quality_task_id=quality_task.task_id)
    return result


def start_claimed_engineering(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task: Task,
    execution: TaskExecution,
    *,
    wait: bool = False,
) -> dict:
    """Run immediately for tests, otherwise enqueue one durable engineering job."""
    if wait:
        return _run_and_publish(
            db,
            user,
            project_id,
            run_id,
            task.task_id,
            execution.execution_id,
        )
    from app.services.workflow_jobs import enqueue_job

    job = enqueue_job(
        db,
        job_type="engineering",
        idempotency_key=f"engineering:{execution.execution_id}",
        project_id=project_id,
        build_run_id=run_id,
        task_id=task.task_id,
        execution_id=execution.execution_id,
        payload={
            "user_id": user.id,
            "project_id": project_id,
            "run_id": run_id,
            "task_id": task.task_id,
            "execution_id": execution.execution_id,
        },
    )
    return {
        "started": job.status in {"queued", "running", "retry_wait"},
        "execution_id": execution.execution_id,
        "job_id": job.job_id,
        "already_running": job.attempt > 0 or job.status == "running",
    }
