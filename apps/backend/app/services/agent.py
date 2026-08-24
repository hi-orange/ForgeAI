from __future__ import annotations

import json
import logging
import uuid

from app.agents import ProductManagerAgent, WebsiteBuilderAgent, WebsiteQualityAgent
from app.agents.website_quality import WebsiteValidationReport, deterministic_issues
from app.core.exceptions import BusinessException
from app.models.project import Project
from app.services.website_editor import ensure_editable_ids

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


def start_website_build(project: Project) -> tuple[str, str, str]:
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
        builder = WebsiteBuilderAgent()
        quality_agent = WebsiteQualityAgent()
        generated_files = ensure_editable_ids(builder.run(approved_spec))
        attempts: list[dict[str, object]] = []
        validation_report = ""

        for attempt in range(3):
            static_issues = deterministic_issues(generated_files)
            if static_issues:
                report = WebsiteValidationReport(passed=False, issues=static_issues)
                source = "deterministic"
            else:
                report = quality_agent.run(approved_spec, generated_files)
                source = "ai_qa"

            attempts.append(
                {
                    "attempt": attempt + 1,
                    "source": source,
                    **report.model_dump(),
                }
            )
            logger.info(
                "Website validation: workflow_id=%s attempt=%s source=%s passed=%s issues=%s",
                workflow_id,
                attempt + 1,
                source,
                report.passed,
                len(report.issues),
            )
            if report.passed:
                validation_report = json.dumps(
                    {"passed": True, "attempts": attempts},
                    ensure_ascii=False,
                    indent=2,
                )
                break
            if attempt == 2:
                raise BusinessException(
                    "网站经过两轮修复后仍未通过质量检查",
                    data={"kind": "validation_failed", "passed": False, "attempts": attempts},
                )

            issues_json = json.dumps(report.model_dump(), ensure_ascii=False, indent=2)
            generated_files = ensure_editable_ids(
                builder.repair(approved_spec, generated_files, issues_json)
            )
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
    return workflow_id, generated_files, validation_report
