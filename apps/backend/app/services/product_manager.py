import hashlib
import json

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents import product_manager as product_manager_agent
from app.agents.prompts.product_manager import APP_SPEC_PROMPT_VERSION
from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.core.settings import settings
from app.models.build_run import BuildRun, BuildRunStage, BuildRunStatus
from app.models.configuration_item import (
    ConfigurationItem,
    ConfigurationItemState,
    ConfigurationItemType,
)
from app.models.plan import Plan, PlanStatus
from app.models.project import Project
from app.models.project_message import ProjectMessage, ProjectMessageSender
from app.models.requirement_clarification import RequirementClarification
from app.models.task import Task, TaskRecipient, TaskStatus
from app.models.task_result import TaskResult
from app.models.user import User
from app.schemas.configuration_item import ConfigurationItemRegistration
from app.schemas.product_manager import (
    MAX_HISTORY_CHARS,
    MAX_HISTORY_MESSAGES,
    ProductManagerInput,
    ProductManagerResult,
    RequirementMessage,
)
from app.services import configuration_manager, task_execution
from app.services import project as project_service
from app.services.requirement_inputs import load_previous_app_spec


def _load_task(
    db: Session, user: User, project_id: int, run_id: str, task_id: str, *, lock: bool = False
) -> tuple[Task, Plan, BuildRun, ProjectMessage]:
    project_service.get_user_project(db, user, project_id)
    statement = (
        select(Task, Plan, BuildRun, ProjectMessage)
        .join(Plan, Task.plan_id == Plan.plan_id)
        .join(
            BuildRun,
            (Plan.build_run_id == BuildRun.run_id) & (Plan.project_id == BuildRun.project_id),
        )
        .join(
            ProjectMessage,
            (Plan.cause_message_id == ProjectMessage.id)
            & (Plan.project_id == ProjectMessage.project_id),
        )
        .where(Plan.project_id == project_id, Plan.build_run_id == run_id, Task.task_id == task_id)
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    row = db.execute(statement).one_or_none()
    if row is None:
        raise NotFoundException("任务或原需求不存在，或不属于指定构建")
    task, plan, run, message = row
    return task, plan, run, message


def _require_running_task(task: Task, plan: Plan, run: BuildRun, message: ProjectMessage) -> None:
    if (
        run.status != BuildRunStatus.RUNNING.value
        or run.stage != BuildRunStage.PM.value
        or run.active_slot != 1
        or plan.status != PlanStatus.RUNNING.value
        or task.status != TaskStatus.RUNNING.value
    ):
        raise ConflictException("只能处理仍在执行中的已领取需求任务")
    if (
        task.recipient != TaskRecipient.PRODUCT_MANAGER.value
        or task.expected_output_type != ConfigurationItemType.APP_SPEC.value
        or task.depends_on_task_ids
        or len(task.input_configuration_item_ids) > 1
    ):
        raise BusinessException("当前只支持无任务依赖、最多一个原需求输入的 ProductManager 任务")
    if message.sender != ProjectMessageSender.USER.value:
        raise BusinessException("需求任务必须引用原始用户消息")


def _load_running_task(
    db: Session, user: User, project_id: int, run_id: str, task_id: str
) -> tuple[Task, Plan, ProjectMessage]:
    task, plan, run, message = _load_task(db, user, project_id, run_id, task_id)
    _require_running_task(task, plan, run, message)
    return task, plan, message


def _prepare_input(
    db: Session, task: Task, message: ProjectMessage, *, lock: bool = False
) -> ProductManagerInput:
    previous = load_previous_app_spec(db, task, lock=lock)
    if previous is not None:
        # 补充任务只读准确的原文档和本次回答，不混入两者之间的其他对话。
        return ProductManagerInput(
            task_instructions=task.instructions,
            source_message=RequirementMessage.model_validate(message),
            recent_messages=[],
            context_truncated=False,
            previous_app_spec=previous,
            previous_item_id=task.input_configuration_item_ids[0],
        )
    statement = (
        select(ProjectMessage)
        .where(
            ProjectMessage.project_id == message.project_id,
            ProjectMessage.sequence < message.sequence,
        )
        .order_by(ProjectMessage.sequence.desc())
        .limit(MAX_HISTORY_MESSAGES + 1)
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    candidates = list(db.scalars(statement).all())
    selected: list[RequirementMessage] = []
    remaining = MAX_HISTORY_CHARS
    for candidate in candidates[:MAX_HISTORY_MESSAGES]:
        # 不截半条消息，也不跳过最近的大消息再选更早的内容，保持连续的最近历史。
        if len(candidate.content) > remaining:
            break
        selected.append(RequirementMessage.model_validate(candidate))
        remaining -= len(candidate.content)
    selected.reverse()
    return ProductManagerInput(
        task_instructions=task.instructions,
        source_message=RequirementMessage.model_validate(message),
        recent_messages=selected,
        context_truncated=len(selected) < len(candidates),
    )


def generate_task_app_spec(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    execution_id: str | None = None,
) -> ProductManagerResult:
    """读取已领取任务，返回有来源的 app_spec 草稿；不领取、登记成果或完成任务。

    使用独立的短只读会话，不提交调用方的待保存修改，不在模型等待期间持有查询事务。
    本步不提供执行幂等或自动重试；重复调用会再次调用模型。登记成果时仍须重新
    检查提交资格，不能把这里的格式校验当作正式发布或任务完成。
    """

    try:
        with Session(bind=db.get_bind(), autoflush=False) as reader:
            task, plan, message = _load_running_task(reader, user, project_id, run_id, task_id)
            task_execution.require_execution(reader, task_id, execution_id)
            payload = _prepare_input(reader, task, message)
            plan_id, cause_message_id = plan.plan_id, plan.cause_message_id
            input_item_ids = list(task.input_configuration_item_ids)
    except ValidationError as exc:
        raise BusinessException("ProductManager 需求输入不符合要求") from exc

    model = settings.deepseek_model
    app_spec = product_manager_agent.generate_app_spec(payload)

    # 模型调用期间可能发生停止或取消。用新的只读事务检查，不读旧会话的缓存/快照。
    # 这里只拒绝已经失去资格的草稿；真正发布时还需要 ConfigurationManager 的事务检查。
    with Session(bind=db.get_bind(), autoflush=False) as reader:
        _load_running_task(reader, user, project_id, run_id, task_id)
        task_execution.require_execution(reader, task_id, execution_id)
    return ProductManagerResult(
        project_id=project_id,
        build_run_id=run_id,
        plan_id=plan_id,
        task_id=task_id,
        cause_message_id=cause_message_id,
        source_message_ids=[message.id for message in payload.recent_messages]
        + [payload.source_message.id],
        context_truncated=payload.context_truncated,
        model=model,
        prompt_version=APP_SPEC_PROMPT_VERSION,
        app_spec=app_spec,
        input_configuration_item_ids=input_item_ids,
        execution_id=execution_id,
    )


def complete_task_app_spec(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    result: ProductManagerResult,
) -> ConfigurationItem:
    """原子登记 app_spec 并完成任务；不调用模型，不宣告整个应用构建成功。

    相同结果可安全重放；同一任务不能改交另一份结果。首次提交必须仍有执行资格，
    不在这里将取消或失效任务当作成功；ConfigurationManager 的独立晚到留档入口不变。
    """

    project_service.get_user_project(db, user, project_id)
    try:
        result = ProductManagerResult.model_validate(result.model_dump())
    except ValidationError as exc:
        raise BusinessException("ProductManager 提交结果不符合要求") from exc
    if (result.project_id, result.build_run_id, result.task_id) != (project_id, run_id, task_id):
        raise BusinessException("提交结果不属于指定的项目、构建或任务")
    content = result.model_dump(mode="json", exclude={"execution_id"})
    if not result.input_configuration_item_ids:
        # 保持迁移前初始结果的哈希不变。
        content.pop("input_configuration_item_ids")
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    try:
        # 与领取和成果登记保持一致：先锁项目，再锁运行，最后锁计划和任务。
        project = db.scalar(
            select(Project)
            .where(Project.id == project_id, Project.user_id == user.id)
            .with_for_update()
        )
        if project is None:
            raise NotFoundException("项目不存在")
        # 不改变运行状态或时间。写锁也让 SQLite 下的并发提交串行检查完整结果。
        locked = db.connection().execute(
            update(BuildRun)
            .where(BuildRun.project_id == project_id, BuildRun.run_id == run_id)
            .values(updated_at=BuildRun.updated_at)
        )
        if locked.rowcount != 1:
            raise NotFoundException("构建任务不存在")
        task, plan, run, message = _load_task(db, user, project_id, run_id, task_id, lock=True)
        if result.plan_id != plan.plan_id or result.cause_message_id != plan.cause_message_id:
            raise BusinessException("提交结果与任务的原计划或原需求不一致")
        if result.input_configuration_item_ids != task.input_configuration_item_ids:
            raise BusinessException("提交结果与任务的原需求版本不一致")
        execution = task_execution.latest_execution(db, task_id, lock=True)
        if execution is not None and execution.execution_id != result.execution_id:
            raise ConflictException("执行编号已失效，不能提交旧执行的结果")

        saved = db.scalar(
            select(TaskResult)
            .where(TaskResult.task_id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if saved is not None:
            if saved.result_hash != result_hash:
                raise ConflictException("该任务已登记不同结果，不能覆盖")
            item = db.scalar(
                select(ConfigurationItem)
                .where(
                    ConfigurationItem.item_id == saved.configuration_item_id,
                    ConfigurationItem.project_id == project_id,
                    ConfigurationItem.producer_run_id == run_id,
                    ConfigurationItem.semantic_type == ConfigurationItemType.APP_SPEC.value,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if item is None:
                raise ConflictException("任务产出关联异常，不能重新登记")
            # 重放只返回原成果，即使运行已结束、成果后来不可用，也不重置任何状态。
        else:
            _require_running_task(task, plan, run, message)
            execution = task_execution.require_execution(
                db, task_id, result.execution_id, lock=True
            )
            other_running_plan = db.scalar(
                select(Plan.plan_id)
                .where(
                    Plan.build_run_id == run_id,
                    Plan.plan_id != plan.plan_id,
                    Plan.status == PlanStatus.RUNNING.value,
                )
                .limit(1)
                .with_for_update()
            )
            if other_running_plan is not None:
                raise ConflictException("该构建有另一份计划在执行，不能混用结果")
            expected = _prepare_input(db, task, message, lock=True)
            expected_ids = [entry.id for entry in expected.recent_messages] + [message.id]
            if (
                result.source_message_ids != expected_ids
                or result.context_truncated != expected.context_truncated
            ):
                raise BusinessException("提交结果的消息来源与原任务上下文不一致")

            item = configuration_manager.stage_configuration_item(
                db,
                project_id=project_id,
                producer_run_id=run_id,
                submission=ConfigurationItemRegistration(
                    semantic_type=ConfigurationItemType.APP_SPEC,
                    schema_version=result.schema_version,
                    payload=result.app_spec.model_dump(mode="json"),
                    upstream_item_ids=list(result.input_configuration_item_ids),
                ),
            )
            if item.state != ConfigurationItemState.USABLE.value:
                raise ConflictException("当前成果没有提交资格，不能完成任务")
            db.add(
                TaskResult(
                    task_id=task_id,
                    configuration_item_id=item.item_id,
                    result_hash=result_hash,
                    source_message_ids=list(result.source_message_ids),
                    context_truncated=result.context_truncated,
                    model=result.model,
                    prompt_version=result.prompt_version,
                )
            )
            task.status = TaskStatus.SUCCEEDED.value
            if execution is not None:
                execution.status = "succeeded"
                execution.active_slot = None
                execution.finished_at = task_execution.utc_now()
            if result.app_spec.open_questions:
                db.add(
                    RequirementClarification(configuration_item_id=item.item_id, task_id=task_id)
                )
            db.flush()
            statuses = list(
                db.scalars(
                    select(Task.status).where(Task.plan_id == plan.plan_id).with_for_update()
                ).all()
            )
            if all(status == TaskStatus.SUCCEEDED.value for status in statuses):
                plan.status = PlanStatus.SUCCEEDED.value
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("成果保存冲突，请重试") from exc
    except Exception:
        db.rollback()
        raise
    db.refresh(item)
    return item
