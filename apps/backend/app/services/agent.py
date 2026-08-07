"""Agent workflow entry points (stub for now)."""

from __future__ import annotations

import logging
import uuid

from app.models.project import Project

logger = logging.getLogger("forgeai.agent")


def start_agent_workflow(project: Project) -> str:
    """Kick off the agent pipeline for a project.

    Returns a workflow id. Real orchestration (LLM / multi-agent) will plug in here.
    """
    workflow_id = f"wf_{uuid.uuid4().hex[:16]}"
    logger.info(
        "Agent workflow started: workflow_id=%s project_id=%s prompt=%s",
        workflow_id,
        project.id,
        (project.prompt or "")[:200],
    )
    return workflow_id
