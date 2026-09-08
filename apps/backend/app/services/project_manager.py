from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents import project_manager as project_manager_agent
from app.agents.prompts.project_manager import (
    CLARIFICATION_TASK_INSTRUCTIONS,
    DESIGN_TASK_INSTRUCTIONS,
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
from app.models.requirement_clarification import RequirementClarification
from app.models.task import Task, TaskRecipient
from app.models.task_result import TaskResult
from app.models.user import User
from app.schemas.app_spec import APP_SPEC_SCHEMA_VERSION, AppSpec
from app.schemas.plan import PlanCreate
from app.schemas.project_message import ProjectMessageCreate
from app.schemas.task import TaskCreate
from app.services import plan as plan_service
from app.services import project as project_service
from app.services.design_handoff import find_pending_design_task, load_design_source
from app.services.project_message import stage_user_project_message
from app.services.task_execution import lock_run

# 喂给模型的上下文上限：条数与总字符，避免 prompt 过长。
MAX_CONTEXT_MESSAGES = 20
MAX_CONTEXT_CHARS = 8000


def create_design_task(db: Session, user: User, project_id: int, run_id: str, item_id: str) -> Task:
    """需求就绪后原子派工；仅创建 pending 计划和任务，不领取或调用模型。"""
    try:
        run = lock_run(db, user, project_id, run_id)
        if (run.status, run.stage, run.active_slot) != ("running", "pm", 1):
            raise ConflictException("当前构建不能进行需求到设计的交接")
        _, _, source_plan, _ = load_design_source(db, project_id, run_id, item_id, lock=True)
        latest = db.scalar(
            select(Plan)
            .where(Plan.project_id == project_id, Plan.build_run_id == run_id)
            .order_by(Plan.version.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if latest is None or latest.version not in (source_plan.version, source_plan.version + 1):
            raise ConflictException("需求已进入其他后续计划，请刷新进度")
        existing = find_pending_design_task(db, source_plan, item_id, lock=True)
        if existing is not None:
            db.commit()
            db.refresh(existing)
            return existing
        plan = plan_service.stage_plan(
            db,
            user,
            project_id,
            run_id,
            PlanCreate(
                version=source_plan.version + 1,
                cause_message_id=source_plan.cause_message_id,
                tasks=[
                    TaskCreate(
                        task_key="system_design",
                        recipient=TaskRecipient.SOLUTION_ARCHITECT,
                        title="根据已确认需求制定技术设计",
                        instructions=DESIGN_TASK_INSTRUCTIONS,
                        expected_output_type=ConfigurationItemType.SYSTEM_DESIGN,
                        input_configuration_item_ids=[item_id],
                    )
                ],
            ),
        )
        task = db.scalar(select(Task).where(Task.plan_id == plan.plan_id))
        assert task is not None
        db.commit()
        db.refresh(task)
        return task
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("设计派工保存冲突，请重试") from exc
    except Exception:
        db.rollback()
        raise


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


def create_clarification_plan(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    item_id: str,
    answer: ProjectMessageCreate,
) -> Plan:
    """原子保存补充回答、新计划及关联；一版原需求最多接受一份回答。"""

    answer = ProjectMessageCreate.model_validate(answer.model_dump())
    try:
        run = lock_run(db, user, project_id, run_id)
        if (run.status, run.stage, run.active_slot) != ("running", "pm", 1):
            raise ConflictException("当前构建不能继续补充需求")
        row = db.execute(
            select(ConfigurationItem, Task, Plan)
            .join(TaskResult, TaskResult.configuration_item_id == ConfigurationItem.item_id)
            .join(Task, Task.task_id == TaskResult.task_id)
            .join(Plan, Plan.plan_id == Task.plan_id)
            .where(
                ConfigurationItem.item_id == item_id,
                ConfigurationItem.project_id == project_id,
                ConfigurationItem.producer_run_id == run_id,
                Plan.project_id == project_id,
                Plan.build_run_id == run_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            raise NotFoundException("待补充的需求不存在或不属于当前构建")
        item, old_task, old_plan = row
        if (
            (item.semantic_type, item.state, item.schema_version)
            != ("app_spec", "usable", APP_SPEC_SCHEMA_VERSION)
            or old_task.status != "succeeded"
            or old_plan.status != "succeeded"
        ):
            raise ConflictException("只有已完成的可用需求能够接受补充回答")
        try:
            spec = AppSpec.model_validate(item.payload)
        except ValidationError as exc:
            raise ConflictException("原需求正文不符合要求") from exc
        if not spec.open_questions:
            raise ConflictException("这版需求没有待确认问题")
        clarification = db.scalar(
            select(RequirementClarification)
            .where(RequirementClarification.configuration_item_id == item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if clarification is not None and clarification.answer_message_id is not None:
            previous_answer = db.scalar(
                select(ProjectMessage)
                .where(ProjectMessage.id == clarification.answer_message_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if previous_answer is None or (
                previous_answer.client_message_id != answer.client_message_id
                or previous_answer.content != answer.content
            ):
                raise ConflictException("这版需求已收到另一份回答，请刷新后继续")
            replay = db.scalar(
                select(Plan)
                .where(Plan.plan_id == clarification.followup_plan_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if replay is None:
                raise ConflictException("补充计划关联异常")
            db.commit()
            return replay
        latest_plan_id = db.scalar(
            select(Plan.plan_id)
            .where(Plan.build_run_id == run_id, Plan.project_id == project_id)
            .order_by(Plan.version.desc())
            .limit(1)
            .with_for_update()
        )
        if latest_plan_id != old_plan.plan_id:
            raise ConflictException("这版需求已被后续计划替代，请刷新后回答")
        message = stage_user_project_message(db, user, project_id, answer)
        original = db.get(ProjectMessage, old_plan.cause_message_id)
        if original is None or message.sequence <= original.sequence:
            raise BusinessException("补充回答必须晚于原需求消息")
        plan = plan_service.stage_plan(
            db,
            user,
            project_id,
            run_id,
            PlanCreate(
                version=old_plan.version + 1,
                cause_message_id=message.id,
                tasks=[
                    TaskCreate(
                        task_key="requirements",
                        recipient=TaskRecipient.PRODUCT_MANAGER,
                        title="根据用户补充完善需求",
                        instructions=CLARIFICATION_TASK_INSTRUCTIONS,
                        expected_output_type=ConfigurationItemType.APP_SPEC,
                        input_configuration_item_ids=[item_id],
                    )
                ],
            ),
        )
        # 兼容迁移前已保存且有待确认问题的 app_spec，首次回答时建立关联。
        if clarification is None:
            clarification = RequirementClarification(
                configuration_item_id=item_id, task_id=old_task.task_id
            )
            db.add(clarification)
        clarification.answer_message_id = message.id
        clarification.followup_plan_id = plan.plan_id
        db.commit()
        db.refresh(plan)
        return plan
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("补充回答保存冲突，请使用相同请求重试") from exc
    except Exception:
        db.rollback()
        raise
