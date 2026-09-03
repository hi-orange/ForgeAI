from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents import project_manager as project_manager_agent
from app.agents.prompts.project_manager import MESSAGE_CLASSIFICATION_PROMPT_VERSION
from app.core.exceptions import BusinessException, NotFoundException
from app.core.settings import settings
from app.models.project_message import ProjectMessage, ProjectMessageSender
from app.models.project_message_classification import ProjectMessageClassification
from app.models.user import User
from app.services import project as project_service

# 喂给模型的上下文上限：条数与总字符，避免 prompt 过长。
MAX_CONTEXT_MESSAGES = 20
MAX_CONTEXT_CHARS = 8000


def _get_project_message(db: Session, project_id: int, message_id: int) -> ProjectMessage:
    """在指定项目内查找消息；跨项目的 message_id 视为不存在。"""

    message = db.scalar(
        select(ProjectMessage).where(
            ProjectMessage.id == message_id,
            ProjectMessage.project_id == project_id,
        )
    )
    if message is None:
        raise NotFoundException("项目消息不存在")
    return message


def _get_classification(
    db: Session,
    message_id: int,
) -> ProjectMessageClassification | None:
    """一条消息最多对应一条分类结果。"""

    return db.scalar(
        select(ProjectMessageClassification).where(
            ProjectMessageClassification.message_id == message_id
        )
    )


def _recent_context(
    db: Session,
    message: ProjectMessage,
) -> list[project_manager_agent.ProjectMessageContext]:
    """取待分类消息之前的最近对话，供模型理解「这个」「还是不对」等指代。"""

    # 先按 sequence 倒序取最近 N 条，再在内存里正序返回。
    candidates = list(
        db.scalars(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == message.project_id,
                ProjectMessage.sequence < message.sequence,
            )
            .order_by(ProjectMessage.sequence.desc())
            .limit(MAX_CONTEXT_MESSAGES)
        ).all()
    )

    remaining_chars = MAX_CONTEXT_CHARS
    context: list[project_manager_agent.ProjectMessageContext] = []
    for candidate in candidates:
        if remaining_chars <= 0:
            break
        # 从最近的消息优先占用额度；单条过长时截断。
        content = candidate.content[:remaining_chars]
        context.append(
            {
                "sequence": candidate.sequence,
                "sender": candidate.sender,
                "content": content,
            }
        )
        remaining_chars -= len(content)

    context.reverse()
    return context


def classify_user_message(
    db: Session,
    user: User,
    project_id: int,
    message_id: int,
) -> ProjectMessageClassification:
    """分类一条已保存的用户消息，但不触发任何后续构建动作。"""

    # 先按 user_id 校验项目归属，避免泄漏他人项目的消息编号。
    project = project_service.get_user_project(db, user, project_id)
    message = _get_project_message(db, project_id, message_id)
    if message.sender != ProjectMessageSender.USER.value:
        raise BusinessException("只能分类用户消息")

    # 已有结果直接返回，避免重复调用付费模型。
    existing = _get_classification(db, message.id)
    if existing is not None:
        return existing

    decision = project_manager_agent.classify_message(
        project_name=project.name,
        project_status=project.status,
        recent_messages=_recent_context(db, message),
        message_sequence=message.sequence,
        message_content=message.content,
    )
    classification = ProjectMessageClassification(
        message_id=message.id,
        category=decision.category.value,
        decision_summary=decision.decision_summary,
        # 记录模型与 prompt 版本，便于事后对照分类质量。
        classifier_model=settings.deepseek_model,
        prompt_version=MESSAGE_CLASSIFICATION_PROMPT_VERSION,
    )
    db.add(classification)
    try:
        db.commit()
    except IntegrityError:
        # 同一消息被并发分类时，只保留最先成功写入的结果。
        db.rollback()
        existing = _get_classification(db, message.id)
        if existing is not None:
            return existing
        raise

    db.refresh(classification)
    return classification


def get_user_message_classification(
    db: Session,
    user: User,
    project_id: int,
    message_id: int,
) -> ProjectMessageClassification:
    """读取已落库的分类结果；尚未分类时返回 404。"""

    project_service.get_user_project(db, user, project_id)
    message = _get_project_message(db, project_id, message_id)
    classification = _get_classification(db, message.id)
    if classification is None:
        raise NotFoundException("消息尚未分类")
    return classification
