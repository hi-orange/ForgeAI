from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, NotFoundException
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectStart
from app.services import agent as agent_service


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
    db.add(project)
    db.commit()
    db.refresh(project)

    workflow_id = agent_service.start_agent_workflow(project)
    return project, workflow_id
