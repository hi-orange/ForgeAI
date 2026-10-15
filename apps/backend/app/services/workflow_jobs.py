from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException
from app.core.settings import settings
from app.models.agent_decision_event import AgentDecisionEvent
from app.models.build_run import BuildRun
from app.models.deployment import Deployment
from app.models.leader_dispatch_checkpoint import LeaderDispatchCheckpoint
from app.models.plan import Plan
from app.models.project import Project
from app.models.task import Task, TaskRecipient
from app.models.task_execution import TaskExecution
from app.models.workflow_job import WorkflowJob
from app.schemas.leader import NextAction
from app.services.task_execution import EXECUTION_LEASE, utc_now
from app.services.workflow_events import append_event, sync_checkpoint_activity_events

ACTIVE_JOB_STATUSES = ("queued", "running", "retry_wait")
PAUSED_JOB_ERROR = "用户离开项目页面，构建已暂停。"
ROLE_JOB_TYPES = {
    TaskRecipient.PRODUCT_MANAGER.value: "product_manager",
    TaskRecipient.ARCHITECT.value: "architect",
    TaskRecipient.CODE_ENGINEER.value: "engineering",
    TaskRecipient.TEST_ENGINEER.value: "quality",
}
logger = logging.getLogger("forgeai.workflow")


def enqueue_job(
    db: Session,
    *,
    job_type: str,
    idempotency_key: str,
    project_id: int,
    payload: dict[str, Any],
    build_run_id: str | None = None,
    task_id: str | None = None,
    execution_id: str | None = None,
    deployment_id: str | None = None,
    commit: bool = True,
) -> WorkflowJob:
    existing = db.scalar(select(WorkflowJob).where(WorkflowJob.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    job = WorkflowJob(
        job_id=f"job_{uuid4().hex}",
        idempotency_key=idempotency_key,
        job_type=job_type,
        project_id=project_id,
        build_run_id=build_run_id,
        task_id=task_id,
        execution_id=execution_id,
        deployment_id=deployment_id,
        payload=dict(payload),
        status="queued",
        attempt=0,
        max_attempts=settings.workflow_job_max_attempts,
        available_at=utc_now(),
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(WorkflowJob).where(WorkflowJob.idempotency_key == idempotency_key)
        )
        if existing is None:
            raise
        return existing
    append_event(
        db,
        idempotency_key=f"job:{job.job_id}:queued",
        stream_key=f"job:{job.job_id}",
        event_type="job_queued",
        project_id=project_id,
        build_run_id=build_run_id,
        task_id=task_id,
        execution_id=execution_id,
        job_id=job.job_id,
        payload={"job_type": job_type},
    )
    if commit:
        db.commit()
        db.refresh(job)
    return job


def claim_next_job(db: Session, worker_id: str) -> WorkflowJob | None:
    """Atomically claim one due job; the conditional UPDATE also fences SQLite races."""

    now = utc_now()
    candidate_ids = list(
        db.scalars(
            select(WorkflowJob.job_id)
            .outerjoin(BuildRun, BuildRun.run_id == WorkflowJob.build_run_id)
            .where(
                WorkflowJob.status.in_(("queued", "retry_wait")),
                WorkflowJob.available_at <= now,
                BuildRun.paused_at.is_(None),
            )
            .order_by(WorkflowJob.available_at, WorkflowJob.created_at)
            .limit(8)
        ).all()
    )
    for job_id in candidate_ids:
        claimed = cast(
            CursorResult[Any],
            db.execute(
                update(WorkflowJob)
                .where(
                    WorkflowJob.job_id == job_id,
                    WorkflowJob.status.in_(("queued", "retry_wait")),
                    WorkflowJob.available_at <= now,
                    or_(
                        WorkflowJob.build_run_id.is_(None),
                        WorkflowJob.build_run_id.in_(
                            select(BuildRun.run_id).where(BuildRun.paused_at.is_(None))
                        ),
                    ),
                )
                .values(
                    status="running",
                    attempt=WorkflowJob.attempt + 1,
                    lease_owner=worker_id,
                    lease_expires_at=now + timedelta(seconds=settings.workflow_job_lease_seconds),
                    last_heartbeat_at=now,
                    started_at=now,
                    finished_at=None,
                    error=None,
                )
            ),
        )
        if claimed.rowcount != 1:
            db.rollback()
            continue
        job = db.get(WorkflowJob, job_id)
        assert job is not None
        append_event(
            db,
            idempotency_key=f"job:{job.job_id}:attempt:{job.attempt}:claimed",
            stream_key=f"job:{job.job_id}",
            event_type="job_claimed",
            project_id=job.project_id,
            build_run_id=job.build_run_id,
            task_id=job.task_id,
            execution_id=job.execution_id,
            job_id=job.job_id,
            payload={"worker_id": worker_id, "attempt": job.attempt},
        )
        db.commit()
        db.refresh(job)
        return job
    return None


def heartbeat_job(db: Session, job_id: str, worker_id: str) -> None:
    now = utc_now()
    renewed = cast(
        CursorResult[Any],
        db.execute(
            update(WorkflowJob)
            .where(
                WorkflowJob.job_id == job_id,
                WorkflowJob.status == "running",
                WorkflowJob.lease_owner == worker_id,
                WorkflowJob.lease_expires_at > now,
            )
            .values(
                lease_expires_at=now + timedelta(seconds=settings.workflow_job_lease_seconds),
                last_heartbeat_at=now,
            )
        ),
    )
    if renewed.rowcount != 1:
        db.rollback()
        raise ConflictException("工作任务租约已失效")
    job = db.get(WorkflowJob, job_id)
    if job is not None and job.execution_id is not None:
        db.execute(
            update(TaskExecution)
            .where(
                TaskExecution.execution_id == job.execution_id,
                TaskExecution.status == "running",
                TaskExecution.active_slot == 1,
            )
            .values(expires_at=now + EXECUTION_LEASE)
        )
    db.commit()


def attach_execution(db: Session, job_id: str, worker_id: str, execution: TaskExecution) -> None:
    job = db.get(WorkflowJob, job_id)
    if job is None or job.status != "running" or job.lease_owner != worker_id:
        raise ConflictException("工作任务租约已失效")
    job.execution_id = execution.execution_id
    payload = dict(job.payload)
    payload.pop("recovery_execution_id", None)
    job.payload = payload
    append_event(
        db,
        idempotency_key=f"job:{job.job_id}:attempt:{job.attempt}:execution:{execution.execution_id}",
        stream_key=f"job:{job.job_id}",
        event_type="execution_attached",
        project_id=job.project_id,
        build_run_id=job.build_run_id,
        task_id=job.task_id,
        execution_id=execution.execution_id,
        job_id=job.job_id,
        payload={"execution_id": execution.execution_id, "attempt": execution.attempt},
    )
    db.commit()


def complete_job(db: Session, job_id: str, worker_id: str) -> None:
    job = db.get(WorkflowJob, job_id)
    if job is None or job.status != "running" or job.lease_owner != worker_id:
        raise ConflictException("工作任务租约已失效")
    job.status = "succeeded"
    job.finished_at = utc_now()
    job.lease_owner = None
    job.lease_expires_at = None
    append_event(
        db,
        idempotency_key=f"job:{job.job_id}:attempt:{job.attempt}:succeeded",
        stream_key=f"job:{job.job_id}",
        event_type="job_succeeded",
        project_id=job.project_id,
        build_run_id=job.build_run_id,
        task_id=job.task_id,
        execution_id=job.execution_id,
        job_id=job.job_id,
        payload={"attempt": job.attempt},
    )
    db.commit()


def fail_job(db: Session, job_id: str, worker_id: str, error: Exception) -> None:
    job = db.get(WorkflowJob, job_id)
    if job is None or job.status != "running" or job.lease_owner != worker_id:
        return
    message = (str(error).strip() or error.__class__.__name__)[:4000]
    execution_id = job.execution_id
    if execution_id is None and job.task_id is not None:
        execution_id = db.scalar(
            select(TaskExecution.execution_id)
            .where(
                TaskExecution.task_id == job.task_id,
                TaskExecution.status == "running",
                TaskExecution.active_slot == 1,
            )
            .order_by(TaskExecution.attempt.desc())
            .limit(1)
        )
    if execution_id is not None:
        execution = db.get(TaskExecution, execution_id)
        if execution is not None and execution.status == "running":
            sync_checkpoint_activity_events(db, execution)
            execution.status = "failed"
            execution.active_slot = None
            execution.finished_at = utc_now()
            execution.error = message[:500]
            payload = dict(job.payload)
            payload["recovery_execution_id"] = execution.execution_id
            job.payload = payload
            job.execution_id = execution.execution_id
    retryable = getattr(error, "retryable", True) is not False
    terminal = not retryable or job.attempt >= job.max_attempts
    job.status = "failed" if terminal else "retry_wait"
    job.available_at = utc_now() + timedelta(seconds=min(60, 2**job.attempt))
    job.finished_at = utc_now() if terminal else None
    job.lease_owner = None
    job.lease_expires_at = None
    job.error = message
    append_event(
        db,
        idempotency_key=f"job:{job.job_id}:attempt:{job.attempt}:failed",
        stream_key=f"job:{job.job_id}",
        event_type="job_failed" if terminal else "job_retry_scheduled",
        project_id=job.project_id,
        build_run_id=job.build_run_id,
        task_id=job.task_id,
        execution_id=execution_id,
        job_id=job.job_id,
        payload={"attempt": job.attempt, "error": message, "retryable": retryable},
    )
    db.commit()
    logger.warning(
        "workflow job %s job_id=%s project=%s run=%s task=%s execution=%s "
        "attempt=%s max_attempts=%s retryable=%s next_retry_at=%s error=%s",
        "failed" if terminal else "retry_scheduled",
        job.job_id,
        job.project_id,
        job.build_run_id,
        job.task_id,
        job.execution_id,
        job.attempt,
        job.max_attempts,
        retryable,
        None if terminal else job.available_at.isoformat() + "Z",
        message,
    )


def recover_expired_jobs(db: Session) -> int:
    """Fence stale executions and make their durable jobs claimable again."""

    now = utc_now()
    stale = list(
        db.scalars(
            select(WorkflowJob)
            .where(
                WorkflowJob.status == "running",
                WorkflowJob.lease_expires_at.is_not(None),
                WorkflowJob.lease_expires_at <= now,
            )
            .with_for_update()
        ).all()
    )
    for job in stale:
        previous_execution_id = job.execution_id
        if previous_execution_id is None and job.task_id is not None:
            previous_execution_id = db.scalar(
                select(TaskExecution.execution_id)
                .where(
                    TaskExecution.task_id == job.task_id,
                    TaskExecution.status == "running",
                    TaskExecution.active_slot == 1,
                )
                .order_by(TaskExecution.attempt.desc())
                .limit(1)
            )
            if previous_execution_id is not None:
                job.execution_id = previous_execution_id
        if previous_execution_id:
            execution = db.get(TaskExecution, previous_execution_id)
            if execution is not None and execution.status == "running":
                sync_checkpoint_activity_events(db, execution)
                execution.status = "failed"
                execution.active_slot = None
                execution.finished_at = now
                execution.error = "Worker 租约过期，已安排恢复。"
                payload = dict(job.payload)
                payload["recovery_execution_id"] = execution.execution_id
                job.payload = payload
        terminal = job.attempt >= job.max_attempts
        job.status = "failed" if terminal else "retry_wait"
        job.available_at = now
        job.finished_at = now if terminal else None
        job.lease_owner = None
        job.lease_expires_at = None
        job.error = "Worker 租约过期" if terminal else None
        append_event(
            db,
            idempotency_key=f"job:{job.job_id}:attempt:{job.attempt}:lease_expired",
            stream_key=f"job:{job.job_id}",
            event_type="job_lease_expired",
            project_id=job.project_id,
            build_run_id=job.build_run_id,
            task_id=job.task_id,
            execution_id=previous_execution_id,
            job_id=job.job_id,
            payload={"attempt": job.attempt, "will_retry": not terminal},
        )
    if stale:
        db.commit()
    return len(stale)


def recover_orphaned_work(db: Session) -> int:
    """Convert pre-worker or otherwise orphaned durable state into claimable jobs."""

    now = utc_now()
    orphan_cutoff = now - timedelta(seconds=settings.workflow_orphan_grace_seconds)
    recovered = 0
    rows = db.execute(
        select(TaskExecution, Task, Plan, Project)
        .join(Task, Task.task_id == TaskExecution.task_id)
        .join(Plan, Plan.plan_id == Task.plan_id)
        .join(Project, Project.id == Plan.project_id)
        .join(BuildRun, BuildRun.run_id == Plan.build_run_id)
        .where(
            TaskExecution.status == "running",
            TaskExecution.active_slot == 1,
            TaskExecution.expires_at <= now,
            BuildRun.paused_at.is_(None),
            BuildRun.status == "running",
            BuildRun.active_slot == 1,
        )
    ).all()
    for execution, task, plan, project in rows:
        # Never lock a joined recovery scan: MySQL can lock Project rows before
        # filtering expired executions, deadlocking a healthy checkpoint's FK insert.
        execution = db.scalar(
            select(TaskExecution)
            .where(
                TaskExecution.execution_id == execution.execution_id,
                TaskExecution.status == "running",
                TaskExecution.active_slot == 1,
                TaskExecution.expires_at <= now,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if execution is None:
            continue
        active = db.scalar(
            select(WorkflowJob.job_id).where(
                WorkflowJob.task_id == task.task_id,
                WorkflowJob.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
        if active is not None:
            continue
        draft = execution.draft if isinstance(execution.draft, dict) else {}
        checkpoint = draft.get("checkpoint")
        if isinstance(checkpoint, dict) and checkpoint.get("outcome") == "blocked":
            reason = str(checkpoint.get("blocked_reason") or "").strip()
            if not reason:
                reason = "执行已阻塞，等待用户显式继续。"
            sync_checkpoint_activity_events(db, execution)
            execution.status = "failed"
            execution.active_slot = None
            execution.finished_at = now
            execution.error = reason[:500]
            append_event(
                db,
                idempotency_key=f"execution:{execution.execution_id}:blocked",
                stream_key=f"execution:{execution.execution_id}",
                event_type="execution_blocked",
                project_id=plan.project_id,
                build_run_id=plan.build_run_id,
                task_id=task.task_id,
                execution_id=execution.execution_id,
                payload={
                    "reason": reason[:500],
                    "recovery_skipped": True,
                },
            )
            recovered += 1
            continue
        job_type = ROLE_JOB_TYPES.get(task.recipient)
        if job_type is None:
            continue
        sync_checkpoint_activity_events(db, execution)
        execution.status = "failed"
        execution.active_slot = None
        execution.finished_at = now
        execution.error = "发现孤立执行，已安排恢复。"
        enqueue_job(
            db,
            job_type=job_type,
            idempotency_key=f"recovery:{execution.execution_id}",
            project_id=plan.project_id,
            build_run_id=plan.build_run_id,
            task_id=task.task_id,
            execution_id=execution.execution_id,
            payload={
                "user_id": project.user_id,
                "project_id": plan.project_id,
                "run_id": plan.build_run_id,
                "task_id": task.task_id,
                "message_id": plan.cause_message_id,
                "recovery_execution_id": execution.execution_id,
            },
            commit=False,
        )
        recovered += 1

    pending_rows = db.execute(
        select(Task, Plan, Project)
        .join(Plan, Plan.plan_id == Task.plan_id)
        .join(Project, Project.id == Plan.project_id)
        .join(BuildRun, BuildRun.run_id == Plan.build_run_id)
        .where(
            Task.status == "pending",
            Task.updated_at <= orphan_cutoff,
            Plan.status == "running",
            BuildRun.status == "running",
            BuildRun.active_slot == 1,
            BuildRun.paused_at.is_(None),
        )
    ).all()
    for task, plan, project in pending_rows:
        task = db.scalar(
            select(Task)
            .where(
                Task.task_id == task.task_id,
                Task.status == "pending",
                Task.updated_at <= orphan_cutoff,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if task is None:
            continue
        active = db.scalar(
            select(WorkflowJob.job_id).where(
                WorkflowJob.task_id == task.task_id,
                WorkflowJob.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
        if active is not None:
            continue
        dependency_ids = list(task.depends_on_task_ids or [])
        if dependency_ids:
            succeeded = db.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.task_id.in_(dependency_ids), Task.status == "succeeded")
            )
            if int(succeeded or 0) != len(dependency_ids):
                continue
        job_type = ROLE_JOB_TYPES.get(task.recipient)
        if job_type is None:
            continue
        enqueue_job(
            db,
            job_type=job_type,
            idempotency_key=f"pending-recovery:{task.task_id}",
            project_id=plan.project_id,
            build_run_id=plan.build_run_id,
            task_id=task.task_id,
            payload={
                "user_id": project.user_id,
                "project_id": plan.project_id,
                "run_id": plan.build_run_id,
                "task_id": task.task_id,
                "message_id": plan.cause_message_id,
            },
            commit=False,
        )
        recovered += 1

    deployments = list(
        db.scalars(select(Deployment).where(Deployment.status.in_(("queued", "building")))).all()
    )
    for deployment in deployments:
        active = db.scalar(
            select(WorkflowJob.job_id).where(
                WorkflowJob.deployment_id == deployment.deployment_id,
                WorkflowJob.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
        if active is not None:
            continue
        enqueue_job(
            db,
            job_type="deployment",
            idempotency_key=f"deployment-recovery:{deployment.deployment_id}",
            project_id=deployment.project_id,
            build_run_id=deployment.build_run_id,
            deployment_id=deployment.deployment_id,
            payload={"deployment_id": deployment.deployment_id},
            commit=False,
        )
        recovered += 1
    if recovered:
        db.commit()
    return recovered


def recover_unapplied_decisions(db: Session) -> int:
    """Replay persisted message decisions whose deterministic side effects were interrupted."""

    from app.services import leader_next_action as leader_next_action_service

    cutoff = utc_now() - timedelta(seconds=settings.workflow_orphan_grace_seconds)
    decisions = list(
        db.scalars(
            select(AgentDecisionEvent)
            .where(
                AgentDecisionEvent.status == "decided",
                AgentDecisionEvent.build_run_id.is_not(None),
                AgentDecisionEvent.message_id.is_not(None),
                AgentDecisionEvent.created_at <= cutoff,
            )
            .order_by(AgentDecisionEvent.created_at)
            .with_for_update()
        ).all()
    )
    recovered = 0
    for decision in decisions:
        project = db.get(Project, decision.project_id)
        if project is None:
            continue
        from app.models.user import User

        user = db.get(User, project.user_id)
        if user is None:
            continue
        assert decision.message_id is not None
        assert decision.build_run_id is not None
        try:
            leader_next_action_service.apply_message_next_action(
                db,
                user,
                project_id=decision.project_id,
                run_id=decision.build_run_id,
                message_id=decision.message_id,
                action=NextAction.model_validate(decision.decision_payload),
                category=decision.category,
            )
        except Exception:
            db.rollback()
            logger.exception(
                "failed to recover unapplied decision decision_key=%s",
                decision.decision_key,
            )
            continue
        recovered += 1
    return recovered


def pause_jobs_for_run(
    db: Session,
    build_run_id: str,
    *,
    execution_id: str | None = None,
    commit: bool = False,
) -> int:
    """Release running leases while keeping jobs resumable behind a paused BuildRun."""

    db.execute(
        update(LeaderDispatchCheckpoint)
        .where(LeaderDispatchCheckpoint.build_run_id == build_run_id)
        .values(lease_owner=None, lease_expires_at=None)
    )
    now = utc_now()
    jobs = list(
        db.scalars(
            select(WorkflowJob)
            .where(
                WorkflowJob.build_run_id == build_run_id,
                WorkflowJob.status.in_(ACTIVE_JOB_STATUSES),
            )
            .with_for_update()
        ).all()
    )
    for job in jobs:
        if job.status == "running":
            job.status = "retry_wait"
            job.available_at = now
        job.lease_owner = None
        job.lease_expires_at = None
        job.finished_at = None
        job.error = PAUSED_JOB_ERROR
        if execution_id is not None and job.task_id is not None:
            payload = dict(job.payload)
            payload["recovery_execution_id"] = execution_id
            job.payload = payload
            job.execution_id = execution_id
        append_event(
            db,
            idempotency_key=f"job:{job.job_id}:attempt:{job.attempt}:paused",
            stream_key=f"job:{job.job_id}",
            event_type="job_paused",
            project_id=job.project_id,
            build_run_id=job.build_run_id,
            task_id=job.task_id,
            execution_id=job.execution_id,
            job_id=job.job_id,
            payload={"attempt": job.attempt},
        )
    if commit:
        db.commit()
    return len(jobs)


def cancel_jobs_for_run(db: Session, build_run_id: str, *, commit: bool = False) -> int:
    db.execute(
        update(LeaderDispatchCheckpoint)
        .where(LeaderDispatchCheckpoint.build_run_id == build_run_id)
        .values(lease_owner=None, lease_expires_at=None)
    )
    jobs = list(
        db.scalars(
            select(WorkflowJob)
            .where(
                WorkflowJob.build_run_id == build_run_id,
                WorkflowJob.status.in_(ACTIVE_JOB_STATUSES),
            )
            .with_for_update()
        ).all()
    )
    for job in jobs:
        job.status = "cancelled"
        job.lease_owner = None
        job.lease_expires_at = None
        job.finished_at = utc_now()
        job.error = "用户取消了本次构建。"
        append_event(
            db,
            idempotency_key=f"job:{job.job_id}:cancelled",
            stream_key=f"job:{job.job_id}",
            event_type="job_cancelled",
            project_id=job.project_id,
            build_run_id=job.build_run_id,
            task_id=job.task_id,
            execution_id=job.execution_id,
            job_id=job.job_id,
            payload={"attempt": job.attempt},
        )
    if commit:
        db.commit()
    return len(jobs)
