from __future__ import annotations

import logging
import uuid

from app.agents import ProductManagerAgent, WebsiteBuilderAgent
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


def start_website_build(project: Project) -> tuple[str, str]:
    workflow_id = f"build_{uuid.uuid4().hex[:16]}"
    approved_spec = (project.approved_spec or "").strip()
    if not approved_spec:
        raise BusinessException("项目缺少已批准的网站规格")

    logger.info(
        "Website build started: workflow_id=%s project_id=%s",
        workflow_id,
        project.id,
    )
    try:
        generated_files = WebsiteBuilderAgent().run(approved_spec)
    except BusinessException:
        raise
    except Exception as exc:
        logger.exception(
            "Website Builder failed: workflow_id=%s project_id=%s",
            workflow_id,
            project.id,
        )
        raise BusinessException("Website Builder 执行失败") from exc

    logger.info(
        "Website build finished: workflow_id=%s project_id=%s files_chars=%s",
        workflow_id,
        project.id,
        len(generated_files),
    )
    return workflow_id, generated_files
