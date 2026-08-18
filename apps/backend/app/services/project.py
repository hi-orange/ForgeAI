import json
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.website_spec import WebsiteSpecification
from app.core.exceptions import BusinessException, NotFoundException
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectApproveSpec, ProjectCreate, ProjectStart
from app.services import agent as agent_service


def _name_from_prompt(prompt: str) -> str:
    cleaned = " ".join(prompt.strip().split())
    if len(cleaned) <= 40:
        return cleaned or "未命名项目"
    return f"{cleaned[:40]}…"


def create_project(db: Session, user: User, payload: ProjectCreate) -> Project:
    """Always insert a new project row.

    Home-page rule: one requirement submission => one project.
    Never reuse/merge an existing project because name or prompt matches.
    """
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
    project.built_at = None
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
    if project.status not in {"spec_approved", "build_failed"} or not project.approved_spec:
        raise BusinessException("请先批准网站规格")

    project.status = "building"
    project.build_error = None
    db.add(project)
    db.commit()
    db.refresh(project)

    try:
        _, generated_files = agent_service.start_website_build(project)
        project.generated_files = generated_files
        project.built_at = datetime.now(UTC).replace(tzinfo=None)
        project.status = "completed"
        db.add(project)
        db.commit()
        db.refresh(project)
    except Exception as exc:
        project.status = "build_failed"
        project.build_error = str(exc)[:2000]
        db.add(project)
        db.commit()
        raise

    return project
