from __future__ import annotations

import logging
import uuid

from app.agents import ProductManagerAgent
from app.core.exceptions import BusinessException
from app.models.project import Project

logger = logging.getLogger("forgeai.agent")


def start_agent_workflow(project: Project) -> tuple[str, str]:
    workflow_id = f"wf_{uuid.uuid4().hex[:16]}"
    requirement = (project.prompt or "").strip()
    if not requirement:
        raise BusinessException("项目缺少用户需求，无法启动 Agent")

    logger.info(
        "Agent workflow started: workflow_id=%s project_id=%s",
        workflow_id,
        project.id,
    )

    try:
        prd = ProductManagerAgent().run(requirement, product_name=project.name)
    except BusinessException:
        raise
    except Exception as exc:
        logger.exception(
            "Product Manager Agent failed: workflow_id=%s project_id=%s",
            workflow_id,
            project.id,
        )
        raise BusinessException("Product Manager Agent 执行失败") from exc

    logger.info(
        "Product Manager finished: workflow_id=%s project_id=%s prd_chars=%s",
        workflow_id,
        project.id,
        len(prd),
    )
    return workflow_id, prd
