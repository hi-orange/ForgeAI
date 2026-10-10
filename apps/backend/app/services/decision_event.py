from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException
from app.models.agent_decision_event import AgentDecisionEvent
from app.schemas.leader import NextAction


def get_decision(db: Session, decision_key: str) -> tuple[AgentDecisionEvent, NextAction] | None:
    event = db.scalar(
        select(AgentDecisionEvent).where(AgentDecisionEvent.decision_key == decision_key)
    )
    if event is None:
        return None
    return event, NextAction.model_validate(event.decision_payload)


def record_decision(
    db: Session,
    *,
    decision_key: str,
    project_id: int,
    build_run_id: str | None,
    action: NextAction,
    message_id: int | None = None,
    report_item_id: str | None = None,
    category: str | None = None,
) -> NextAction:
    """Persist once; retries replay the original decision instead of re-deciding."""

    existing = get_decision(db, decision_key)
    if existing is not None:
        return existing[1]
    event = AgentDecisionEvent(
        event_id=f"evt_{uuid4().hex}",
        decision_key=decision_key,
        project_id=project_id,
        build_run_id=build_run_id,
        message_id=message_id,
        report_item_id=report_item_id,
        category=category,
        action=action.action.value,
        reason_code=action.reason_code,
        decided_by=action.decided_by,
        recipient=action.recipient.value if action.recipient else None,
        source_artifact_ids=list(action.source_artifact_ids),
        required_inputs=list(action.required_inputs),
        risk_flags=list(action.risk_flags),
        decision_payload=action.model_dump(mode="json"),
        status="decided",
    )
    db.add(event)
    try:
        db.commit()
    except Exception:
        db.rollback()
        replay = get_decision(db, decision_key)
        if replay is not None:
            return replay[1]
        raise
    return action


def mark_applied(db: Session, decision_key: str) -> None:
    event = db.scalar(
        select(AgentDecisionEvent)
        .where(AgentDecisionEvent.decision_key == decision_key)
        .with_for_update()
    )
    if event is None:
        raise ConflictException("调度决策事件不存在")
    if event.status != "applied":
        event.status = "applied"
        event.applied_at = datetime.now(UTC).replace(tzinfo=None)
        event.error = None
        db.commit()


def mark_failed(db: Session, decision_key: str, error: Exception) -> None:
    db.rollback()
    event = db.scalar(
        select(AgentDecisionEvent)
        .where(AgentDecisionEvent.decision_key == decision_key)
        .with_for_update()
    )
    if event is None:
        return
    event.status = "failed"
    event.error = (str(error).strip() or error.__class__.__name__)[:4000]
    db.commit()


def list_project_decisions(db: Session, project_id: int) -> list[AgentDecisionEvent]:
    return list(
        db.scalars(
            select(AgentDecisionEvent)
            .where(AgentDecisionEvent.project_id == project_id)
            .order_by(AgentDecisionEvent.created_at, AgentDecisionEvent.event_id)
        ).all()
    )
