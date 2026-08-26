from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, NotFoundException
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectCreate


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
