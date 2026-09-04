from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents import project_manager as project_manager_agent
from app.agents.prompts.project_manager import (
    INITIAL_REQUIREMENTS_TASK_INSTRUCTIONS,
    MESSAGE_CLASSIFICATION_PROMPT_VERSION,
)
from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.core.settings import settings
from app.models.configuration_item import ConfigurationItem, ConfigurationItemType
from app.models.plan import Plan
from app.models.project import Project, ProjectStatus
from app.models.project_message import ProjectMessage, ProjectMessageSender
from app.models.project_message_classification import (
    ProjectMessageCategory,
    ProjectMessageClassification,
)
from app.models.task import TaskRecipient
from app.models.user import User
from app.schemas.plan import PlanCreate
from app.schemas.task import TaskCreate
from app.services import plan as plan_service
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


def create_initial_plan(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    message_id: int,
) -> Plan:
    """为已有 BuildRun 安排首次需求整理；只读取已保存的分类，不执行任务。

    新建仅支持尚无正式成果的 draft 项目。同一运行的 Plan v1 可以重放，
    但不能换一条消息覆盖旧计划，也不负责后续需求变更或修复规划。
    """

    project_service.get_user_project(db, user, project_id)
    message = _get_project_message(db, project_id, message_id)
    if message.sender != ProjectMessageSender.USER.value:
        raise BusinessException("只能根据用户消息创建初始计划")
    classification = _get_classification(db, message.id)
    if classification is None:
        raise NotFoundException("消息尚未分类")
    if classification.category != ProjectMessageCategory.PRODUCT_CHANGE.value:
        raise BusinessException("初始计划只接受 product_change 类型的用户消息")

    payload = PlanCreate(
        version=1,
        cause_message_id=message.id,
        tasks=[
            TaskCreate(
                task_key="requirements",
                recipient=TaskRecipient.PRODUCT_MANAGER,
                title="整理应用需求",
                instructions=INITIAL_REQUIREMENTS_TASK_INSTRUCTIONS,
                expected_output_type=ConfigurationItemType.APP_SPEC,
            )
        ],
    )
    try:
        # 与 Plan / ConfigurationManager 一样先锁项目，避免检查后被并发发布的成果绕过。
        project = db.scalar(
            select(Project)
            .where(Project.id == project_id, Project.user_id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            raise NotFoundException("项目不存在")
        plans = list(
            db.scalars(
                select(Plan)
                .where(Plan.project_id == project_id, Plan.build_run_id == run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        is_replay = any(plan.version == 1 for plan in plans)
        if not is_replay:
            if plans:
                raise ConflictException("该 BuildRun 已有计划，不能补建初始计划")
            if project.status != ProjectStatus.DRAFT.value:
                raise ConflictException("项目已有可用版本，不能按首次构建创建计划")
            # 包括不可用成果：本入口不猜测应该复用还是重做已有结果。
            existing_item = db.scalar(
                select(ConfigurationItem.item_id)
                .where(ConfigurationItem.project_id == project_id)
                .limit(1)
                .with_for_update()
            )
            if existing_item is not None:
                raise ConflictException("项目已有正式成果，需要另行制定后续计划")

        # 版本冲突、运行资格、整份计划的原子写入由现有保存服务负责。
        plan = plan_service.save_plan(db, user, project_id, run_id, payload)
        if is_replay:
            # save_plan 的历史重放不提交；释放本入口取得的项目锁。
            db.commit()
        return plan
    except Exception:
        db.rollback()
        raise
