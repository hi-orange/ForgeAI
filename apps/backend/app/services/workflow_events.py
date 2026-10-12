from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.plan import Plan
from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.models.workflow_event import WorkflowEvent


def append_event(
    db: Session,
    *,
    idempotency_key: str,
    stream_key: str,
    event_type: str,
    project_id: int,
    payload: dict[str, Any] | None = None,
    build_run_id: str | None = None,
    task_id: str | None = None,
    execution_id: str | None = None,
    job_id: str | None = None,
) -> WorkflowEvent:
    """Insert one immutable event; replaying the same key returns the original fact."""

    existing = db.scalar(
        select(WorkflowEvent).where(WorkflowEvent.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    sequence = (
        int(
            db.scalar(
                select(func.max(WorkflowEvent.sequence)).where(
                    WorkflowEvent.stream_key == stream_key
                )
            )
            or 0
        )
        + 1
    )
    event = WorkflowEvent(
        event_id=f"wevt_{uuid4().hex}",
        idempotency_key=idempotency_key,
        stream_key=stream_key,
        sequence=sequence,
        event_type=event_type,
        project_id=project_id,
        build_run_id=build_run_id,
        task_id=task_id,
        execution_id=execution_id,
        job_id=job_id,
        payload=dict(payload or {}),
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(WorkflowEvent).where(WorkflowEvent.idempotency_key == idempotency_key)
        )
        if existing is None:
            raise
        return existing
    return event


def sync_checkpoint_activity_events(
    db: Session,
    execution: TaskExecution,
) -> int:
    """Append only the unsynchronized suffix of a mutable recovery checkpoint."""

    draft = execution.draft if isinstance(execution.draft, dict) else None
    checkpoint = draft.get("checkpoint") if draft else None
    if not isinstance(checkpoint, dict):
        return 0
    activity = checkpoint.get("activity")
    if not isinstance(activity, list):
        return 0
    cursor = checkpoint.get("activity_event_cursor", 0)
    cursor = cursor if isinstance(cursor, int) and 0 <= cursor <= len(activity) else 0
    if cursor == len(activity):
        return 0
    row = db.execute(
        select(Task, Plan)
        .join(Plan, Task.plan_id == Plan.plan_id)
        .where(Task.task_id == execution.task_id)
    ).one_or_none()
    if row is None:
        return 0
    task, plan = row
    appended = 0
    for index, raw in enumerate(activity[cursor:], start=cursor + 1):
        if not isinstance(raw, dict):
            continue
        raw_id = str(raw.get("id") or index)
        append_event(
            db,
            idempotency_key=f"activity:{execution.execution_id}:{index}:{raw_id}",
            stream_key=f"execution:{execution.execution_id}",
            event_type="activity",
            project_id=plan.project_id,
            build_run_id=plan.build_run_id,
            task_id=task.task_id,
            execution_id=execution.execution_id,
            payload=dict(raw),
        )
        appended += 1
    checkpoint["activity_event_cursor"] = len(activity)
    assert draft is not None
    execution.draft = dict(draft)
    flag_modified(execution, "draft")
    return appended
