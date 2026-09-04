import hashlib
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.models.build_run import ACTIVE_BUILD_RUN_STATUSES, BuildRun
from app.models.configuration_item import ConfigurationItem, ConfigurationItemState
from app.models.plan import Plan
from app.models.project import Project
from app.models.project_message import ProjectMessage, ProjectMessageSender
from app.models.task import Task
from app.models.user import User
from app.schemas.plan import PlanCreate
from app.services import project as project_service


def _find_version(
    db: Session, project_id: int, run_id: str, version: int, *, lock: bool = False
) -> Plan | None:
    statement = select(Plan).where(
        Plan.project_id == project_id,
        Plan.build_run_id == run_id,
        Plan.version == version,
    )
    if lock:
        # MySQL 可重复读事务中，锁定读取才能看到等待项目锁期间刚提交的版本。
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return db.scalar(statement)


def _replay(existing: Plan, definition_hash: str) -> Plan:
    if existing.definition_hash != definition_hash:
        raise ConflictException("该 Plan 版本已保存了不同内容；请使用新版本")
    return existing


def _lock_project(db: Session, project_id: int) -> None:
    # 与 ConfigurationManager 使用相同的项目锁顺序。
    project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if project is None:
        raise NotFoundException("项目不存在")


def _validate_source(db: Session, project_id: int, run_id: str, message_id: int) -> None:
    run = db.scalar(
        select(BuildRun)
        .where(BuildRun.project_id == project_id, BuildRun.run_id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise NotFoundException("构建任务不存在")
    if run.status not in ACTIVE_BUILD_RUN_STATUSES or run.active_slot != 1:
        raise ConflictException("只能为活动 BuildRun 保存新的 Plan")
    message = db.scalar(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project_id, ProjectMessage.id == message_id
        )
    )
    if message is None:
        raise NotFoundException("项目消息不存在")
    if message.sender != ProjectMessageSender.USER.value:
        raise BusinessException("Plan 必须由用户消息触发")


def _validate_inputs(db: Session, project_id: int, payload: PlanCreate) -> None:
    item_ids = {item_id for task in payload.tasks for item_id in task.input_configuration_item_ids}
    if not item_ids:
        return
    items = list(
        db.scalars(
            select(ConfigurationItem)
            .where(
                ConfigurationItem.project_id == project_id,
                ConfigurationItem.item_id.in_(item_ids),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    if len(items) != len(item_ids):
        raise NotFoundException("输入 ConfigurationItem 不存在或不属于当前项目")
    if any(item.state != ConfigurationItemState.USABLE.value for item in items):
        raise ConflictException("Task 输入不能引用 unusable 的 ConfigurationItem")


def save_plan(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    payload: PlanCreate,
) -> Plan:
    """原子保存 Plan 和全部 Task；同一运行同一版本幂等，不启动任务或切换当前计划。"""

    project_service.get_user_project(db, user, project_id)
    # 调用方可能修改已校验对象中的 list；落库前再验证一次并复制输入。
    payload = PlanCreate.model_validate(payload.model_dump())
    canonical = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    definition_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    existing = _find_version(db, project_id, run_id, payload.version)
    if existing is not None:
        # 重放只读历史结果，即使原运行已结束或输入后来变为 unusable 也不会新增任务。
        return _replay(existing, definition_hash)

    plan = Plan(
        plan_id=f"plan_{uuid4().hex}",
        project_id=project_id,
        build_run_id=run_id,
        version=payload.version,
        cause_message_id=payload.cause_message_id,
        definition_hash=definition_hash,
    )
    task_ids = {task.task_key: f"task_{uuid4().hex}" for task in payload.tasks}
    tasks = [
        Task(
            task_id=task_ids[task.task_key],
            plan_id=plan.plan_id,
            task_key=task.task_key,
            position=position,
            recipient=task.recipient.value,
            title=task.title,
            instructions=task.instructions,
            expected_output_type=task.expected_output_type.value,
            input_configuration_item_ids=list(task.input_configuration_item_ids),
            depends_on_task_ids=[task_ids[key] for key in task.depends_on_task_keys],
        )
        for position, task in enumerate(payload.tasks, start=1)
    ]
    try:
        _lock_project(db, project_id)
        existing = _find_version(db, project_id, run_id, payload.version, lock=True)
        if existing is not None:
            return _replay(existing, definition_hash)
        _validate_source(db, project_id, run_id, payload.cause_message_id)
        _validate_inputs(db, project_id, payload)
        db.add(plan)
        # 不配置会隐式写入数据的 ORM 级联；先插入父表，全部任务仍处于同一事务。
        db.flush()
        db.add_all(tasks)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing = _find_version(db, project_id, run_id, payload.version)
        if existing is not None:
            return _replay(existing, definition_hash)
        raise ConflictException("Plan / Task 保存冲突，请重试") from exc
    except Exception:
        db.rollback()
        raise
    db.refresh(plan)
    return plan


def get_user_plan(db: Session, user: User, project_id: int, plan_id: str) -> Plan:
    project_service.get_user_project(db, user, project_id)
    plan = db.scalar(select(Plan).where(Plan.project_id == project_id, Plan.plan_id == plan_id))
    if plan is None:
        raise NotFoundException("计划不存在")
    return plan


def list_user_plans(db: Session, user: User, project_id: int, run_id: str) -> list[Plan]:
    project_service.get_user_project(db, user, project_id)
    run = db.scalar(
        select(BuildRun).where(BuildRun.project_id == project_id, BuildRun.run_id == run_id)
    )
    if run is None:
        raise NotFoundException("构建任务不存在")
    return list(
        db.scalars(
            select(Plan)
            .where(Plan.project_id == project_id, Plan.build_run_id == run_id)
            .order_by(Plan.version)
        ).all()
    )
