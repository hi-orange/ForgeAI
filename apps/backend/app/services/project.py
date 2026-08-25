import json
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.website_quality import deterministic_issues
from app.agents.website_spec import WebsiteSpecification
from app.core.exceptions import AppException, BusinessException, NotFoundException
from app.models.project import Project
from app.models.user import User
from app.schemas.project import (
    ProjectApproveSpec,
    ProjectCreate,
    ProjectElementAiEdit,
    ProjectElementAiReply,
    ProjectOut,
    ProjectStart,
    ProjectWebsiteEdit,
    ProjectWebsiteRevise,
    ProjectWebsiteReviseReply,
)
from app.services import agent as agent_service
from app.services.website_editor import apply_website_patches


def _name_from_prompt(prompt: str) -> str:
    cleaned = " ".join(prompt.strip().split())
    if len(cleaned) <= 40:
        return cleaned or "未命名项目"
    return f"{cleaned[:40]}…"


def create_project(db: Session, user: User, payload: ProjectCreate) -> Project:
    prompt = payload.prompt.strip()
    if not prompt:
        raise BusinessException("请输入需求")

    project = Project(
        user_id=user.id,
        name=(payload.name or _name_from_prompt(prompt)).strip()[:200],
        description=payload.description,
        prompt=prompt,
        status="draft",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def get_user_project(db: Session, user: User, project_id: int) -> Project:
    project = db.scalar(select(Project).where(Project.id == project_id, Project.user_id == user.id))
    if project is None:
        raise NotFoundException("项目不存在")
    return project


def list_user_projects(db: Session, user: User, *, limit: int = 20) -> list[Project]:
    return list(
        db.scalars(
            select(Project)
            .where(Project.user_id == user.id)
            .order_by(Project.updated_at.desc())
            .limit(limit)
        ).all()
    )


def start_project(
    db: Session,
    user: User,
    project_id: int,
    payload: ProjectStart | None = None,
) -> tuple[Project, str]:
    project = get_user_project(db, user, project_id)

    if payload and payload.prompt and payload.prompt.strip():
        project.prompt = payload.prompt.strip()
        if not project.name or project.name == "未命名项目":
            project.name = _name_from_prompt(project.prompt)

    if not project.prompt or not project.prompt.strip():
        raise BusinessException("项目缺少用户需求，无法启动")

    if project.status == "running":
        raise BusinessException("项目已在构建中")

    project.status = "running"
    project.approved_spec = None
    project.approved_at = None
    project.generated_files = None
    project.build_error = None
    project.validation_report = None
    project.built_at = None
    project.website_revision = 0
    db.add(project)
    db.commit()
    db.refresh(project)

    try:
        workflow_id, prd = agent_service.start_agent_workflow(project)
        project.prd = prd
        project.status = "prd_ready"
        db.add(project)
        db.commit()
        db.refresh(project)
    except Exception:
        project.status = "failed"
        db.add(project)
        db.commit()
        raise

    return project, workflow_id


def approve_project_spec(
    db: Session,
    user: User,
    project_id: int,
    payload: ProjectApproveSpec,
) -> Project:
    project = get_user_project(db, user, project_id)
    if project.status == "spec_approved" and project.approved_spec:
        return project
    if project.status != "prd_ready" or not project.prd:
        raise BusinessException("网站规格尚未生成，无法批准")

    try:
        specification = WebsiteSpecification.model_validate_json(project.prd)
    except (ValidationError, ValueError) as exc:
        raise BusinessException("当前项目使用旧版 PRD，请重新生成网站规格") from exc

    requested = {(item.page_id, item.section_id) for item in payload.selected_sections}
    available = {
        (page.id, section.id) for page in specification.site.pages for section in page.sections
    }
    unknown = requested - available
    if unknown:
        raise BusinessException("选择中包含不存在的页面区块")

    approved_pages = []
    for page in specification.site.pages:
        approved_sections = [
            section for section in page.sections if (page.id, section.id) in requested
        ]
        if approved_sections:
            approved_pages.append(page.model_copy(update={"sections": approved_sections}))

    approved = specification.model_copy(
        update={"site": specification.site.model_copy(update={"pages": approved_pages})}
    )
    project.approved_spec = json.dumps(approved.model_dump(), ensure_ascii=False, indent=2)
    project.approved_at = datetime.now(UTC).replace(tzinfo=None)
    project.status = "spec_approved"
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def build_project(db: Session, user: User, project_id: int) -> Project:
    project = get_user_project(db, user, project_id)
    if project.status == "completed" and project.generated_files:
        return project
    if (
        project.status not in {"spec_approved", "build_failed", "validation_failed"}
        or not project.approved_spec
    ):
        raise BusinessException("请先批准网站规格")

    project.status = "building"
    project.build_error = None
    project.validation_report = None
    db.add(project)
    db.commit()
    db.refresh(project)

    try:
        _, generated_files, validation_report = agent_service.start_website_build(project)
        project.generated_files = generated_files
        project.website_revision = 1
        project.validation_report = validation_report
        project.built_at = datetime.now(UTC).replace(tzinfo=None)
        project.status = "completed"
        db.add(project)
        db.commit()
        db.refresh(project)
    except Exception as exc:
        validation_data = exc.data if isinstance(exc, AppException) else None
        is_validation_failure = (
            isinstance(validation_data, dict) and validation_data.get("kind") == "validation_failed"
        )
        project.status = "validation_failed" if is_validation_failure else "build_failed"
        project.build_error = str(exc)[:2000]
        if validation_data:
            project.validation_report = json.dumps(validation_data, ensure_ascii=False, indent=2)
        db.add(project)
        db.commit()
        raise

    return project


def edit_project_website(
    db: Session,
    user: User,
    project_id: int,
    payload: ProjectWebsiteEdit,
) -> Project:
    project = get_user_project(db, user, project_id)
    if project.status != "completed" or not project.generated_files:
        raise BusinessException("网站尚未构建完成，无法编辑")
    if payload.base_revision != project.website_revision:
        raise BusinessException(
            "网站版本已更新，请刷新后重试",
            data={"current_revision": project.website_revision},
        )

    updated_files = apply_website_patches(project.generated_files, payload.patches)
    issues = deterministic_issues(updated_files)
    if issues:
        raise BusinessException(
            "修改后的页面未通过基础检查",
            data={"issues": [issue.model_dump() for issue in issues]},
        )

    project.generated_files = updated_files
    project.website_revision += 1
    project.updated_at = datetime.now(UTC).replace(tzinfo=None)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def suggest_project_element_edit(
    db: Session,
    user: User,
    project_id: int,
    payload: ProjectElementAiEdit,
) -> ProjectElementAiReply:
    """Compat: Design Ask used to be a separate agent; now maps onto website revise."""
    from app.schemas.project import ProjectWebsiteRevise, ProjectWebsiteReviseFocus

    revise_reply = revise_project_website(
        db,
        user,
        project_id,
        ProjectWebsiteRevise(
            instruction=payload.instruction,
            history=payload.history,
            base_revision=payload.base_revision,
            focus=ProjectWebsiteReviseFocus(
                element_id=payload.element_id,
                tag_name=payload.tag_name,
                text=payload.text,
                text_editable=payload.text_editable,
                styles=payload.styles,
            ),
        ),
    )
    return ProjectElementAiReply(
        mode=revise_reply.mode,
        message=revise_reply.message,
        project=revise_reply.project,
    )


def revise_project_website(
    db: Session,
    user: User,
    project_id: int,
    payload: ProjectWebsiteRevise,
) -> ProjectWebsiteReviseReply:
    from app.agents.site_reviser import suggest_site_revise_reply

    project = get_user_project(db, user, project_id)
    if project.status != "completed" or not project.generated_files:
        raise BusinessException("网站尚未构建完成，无法修改")

    reply = suggest_site_revise_reply(
        payload,
        current_files=project.generated_files,
        approved_spec=project.approved_spec,
    )
    if reply.mode != "applied" or not reply.files_json:
        return ProjectWebsiteReviseReply(mode="message", message=reply.message)

    if payload.base_revision != project.website_revision:
        raise BusinessException(
            "网站版本已更新，请刷新后重试",
            data={"current_revision": project.website_revision},
        )

    issues = deterministic_issues(reply.files_json)
    if issues:
        detail = "；".join(issue.description for issue in issues[:3])
        return ProjectWebsiteReviseReply(
            mode="message",
            message=f"改动没通过基础检查：{detail}。换一种改法试试。",
        )

    project.generated_files = reply.files_json
    project.website_revision += 1
    project.updated_at = datetime.now(UTC).replace(tzinfo=None)
    db.add(project)
    db.commit()
    db.refresh(project)

    return ProjectWebsiteReviseReply(
        mode="applied",
        message=reply.message,
        project=ProjectOut.model_validate(project),
    )
