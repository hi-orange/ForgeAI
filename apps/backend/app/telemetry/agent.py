from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.agent_usage_event import AgentUsageEvent
from app.schemas.agent_action import ChatWithToolsResult

logger = logging.getLogger("forgeai.telemetry")


@dataclass(frozen=True, slots=True)
class AgentTelemetryContext:
    project_id: int
    build_run_id: str | None
    task_id: str | None
    execution_id: str | None
    role: str
    bind: Engine | Connection


_context: ContextVar[AgentTelemetryContext | None] = ContextVar(
    "forgeai_agent_telemetry", default=None
)


@contextmanager
def agent_telemetry_scope(
    *,
    project_id: int,
    build_run_id: str | None,
    task_id: str | None,
    execution_id: str | None,
    role: str,
    bind: Engine | Connection,
):
    token = _context.set(
        AgentTelemetryContext(project_id, build_run_id, task_id, execution_id, role, bind)
    )
    try:
        yield
    finally:
        _context.reset(token)


def _estimated_cost_microusd(result: ChatWithToolsResult) -> int | None:
    usage = result.usage
    input_rate = settings.llm_input_cost_per_million_usd
    output_rate = settings.llm_output_cost_per_million_usd
    if usage is None or input_rate is None or output_rate is None:
        return None
    return round(
        int(usage.prompt_tokens or 0) * input_rate + int(usage.completion_tokens or 0) * output_rate
    )


def record_model_turn(result: ChatWithToolsResult, duration_ms: int) -> None:
    context = _context.get()
    if context is None:
        return
    usage = result.usage
    try:
        with Session(bind=context.bind) as db:
            db.add(
                AgentUsageEvent(
                    event_id=f"use_{uuid4().hex}",
                    project_id=context.project_id,
                    build_run_id=context.build_run_id,
                    task_id=context.task_id,
                    execution_id=context.execution_id,
                    role=context.role,
                    model=result.model,
                    protocol=result.protocol,
                    prompt_tokens=usage.prompt_tokens if usage else None,
                    completion_tokens=usage.completion_tokens if usage else None,
                    total_tokens=usage.total_tokens if usage else None,
                    duration_ms=max(0, duration_ms),
                    estimated_cost_microusd=_estimated_cost_microusd(result),
                )
            )
            db.commit()
    except Exception:  # noqa: BLE001 - telemetry must never break delivery
        logger.exception("failed to persist agent usage telemetry")
