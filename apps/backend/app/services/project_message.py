from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException
from app.models.project_message import ProjectMessage, ProjectMessageSender
from app.models.user import User
from app.schemas.project_message import ProjectMessageCreate
from app.services import project as project_service

MAX_SEQUENCE_RETRIES = 5


def _find_by_client_message_id(
    db: Session,
    project_id: int,
    client_message_id: str,
) -> ProjectMessage | None:
    return db.scalar(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.client_message_id == client_message_id,
        )
    )


def _next_sequence(db: Session, project_id: int) -> int:
    value = db.scalar(
        select(func.coalesce(func.max(ProjectMessage.sequence), 0) + 1).where(
            ProjectMessage.project_id == project_id
        )
    )
    return int(value or 1)


def _validate_idempotent_replay(existing: ProjectMessage, content: str) -> ProjectMessage:
    if existing.sender != ProjectMessageSender.USER.value or existing.content != content:
        raise ConflictException("client_message_id 已用于另一条消息")
    return existing


def create_user_project_message(
    db: Session,
    user: User,
    project_id: int,
    payload: ProjectMessageCreate,
) -> ProjectMessage:
    """追加一条用户消息，并用项目内唯一键保证重试不会重复写入。"""

    project_service.get_user_project(db, user, project_id)
    content = payload.content
    client_message_id = payload.client_message_id

    existing = _find_by_client_message_id(db, project_id, client_message_id)
    if existing is not None:
        return _validate_idempotent_replay(existing, content)

    last_error: IntegrityError | None = None
    for _ in range(MAX_SEQUENCE_RETRIES):
        message = ProjectMessage(
            project_id=project_id,
            sequence=_next_sequence(db, project_id),
            sender=ProjectMessageSender.USER.value,
            content=content,
            client_message_id=client_message_id,
        )
        db.add(message)
        try:
            db.commit()
        except IntegrityError as exc:
            # 并发请求可能争用同一个 sequence，或同时重放同一个 client_message_id。
            db.rollback()
            last_error = exc
            existing = _find_by_client_message_id(db, project_id, client_message_id)
            if existing is not None:
                return _validate_idempotent_replay(existing, content)
            continue

        db.refresh(message)
        return message

    raise ConflictException("消息写入冲突，请重试") from last_error


def list_user_project_messages(
    db: Session,
    user: User,
    project_id: int,
    *,
    after_sequence: int = 0,
    limit: int = 100,
) -> list[ProjectMessage]:
    """按项目内 sequence 正序读取当前用户可见的对话。"""

    project_service.get_user_project(db, user, project_id)
    return list(
        db.scalars(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.sequence > after_sequence,
            )
            .order_by(ProjectMessage.sequence.asc())
            .limit(limit)
        ).all()
    )
