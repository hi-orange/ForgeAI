from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException, NotFoundException
from app.models.build_run import ACTIVE_BUILD_RUN_STATUSES, BuildRun, BuildRunStatus
from app.models.configuration_item import ConfigurationItem, ConfigurationItemType
from app.models.plan import Plan
from app.models.project import Project
from app.models.run_revision import RunRevision
from app.models.task import TaskRecipient
from app.models.task_result import TaskResult
from app.models.user import User
from app.schemas.plan import PlanCreate
from app.schemas.task import TaskCreate
from app.services import plan as plan_service
from app.services import project as project_service
from app.services.engineering import APPROVAL_VERSION

PRODUCT_REVISION_INSTRUCTIONS = (
    "基于固定的上一版已批准 app_spec 和本次用户消息，生成新的完整 app_spec。"
    "保留未被修改的产品意图；用户可见变化必须重新等待明确批准。"
)


def get_revision_for_target(db: Session, project_id: int, run_id: str) -> RunRevision | None:
    return db.scalar(
        select(RunRevision).where(
            RunRevision.project_id == project_id,
            RunRevision.target_run_id == run_id,
        )
    )


def _approved_baseline(db: Session, project_id: int) -> ConfigurationItem:
    item = db.scalar(
        select(ConfigurationItem)
        .join(TaskResult, TaskResult.configuration_item_id == ConfigurationItem.item_id)
        .where(
            ConfigurationItem.project_id == project_id,
            ConfigurationItem.semantic_type == ConfigurationItemType.APP_SPEC.value,
            ConfigurationItem.state == "usable",
            TaskResult.prompt_version == APPROVAL_VERSION,
        )
        .order_by(ConfigurationItem.id.desc())
        .limit(1)
    )
    if item is None:
        raise ConflictException("项目还没有可作为修订基线的已批准需求")
    return item


def _latest_code(db: Session, project_id: int, source_run_id: str) -> ConfigurationItem | None:
    return db.scalar(
        select(ConfigurationItem)
        .where(
            ConfigurationItem.project_id == project_id,
            ConfigurationItem.producer_run_id == source_run_id,
            ConfigurationItem.semantic_type == ConfigurationItemType.CODE.value,
            ConfigurationItem.state == "usable",
        )
        .order_by(ConfigurationItem.id.desc())
        .limit(1)
    )


def create_revision_run(
    db: Session,
    user: User,
    project_id: int,
    *,
    source_run_id: str,
    cause_message_id: int,
    kind: str,
) -> BuildRun:
    """Create the target BuildRun and its immutable baseline edge in one transaction."""

    if kind not in {"product_change", "implementation_repair"}:
        raise ValueError("unsupported revision kind")
    project_service.get_user_project(db, user, project_id)
    try:
        project = db.scalar(
            select(Project)
            .where(Project.id == project_id, Project.user_id == user.id)
            .with_for_update()
        )
        if project is None:
            raise NotFoundException("项目不存在")
        replay = db.scalar(
            select(RunRevision).where(RunRevision.cause_message_id == cause_message_id)
        )
        if replay is not None:
            run = db.scalar(select(BuildRun).where(BuildRun.run_id == replay.target_run_id))
            if run is None:
                raise ConflictException("修订运行关联损坏")
            db.commit()
            return run
        active = db.scalar(
            select(BuildRun)
            .where(
                BuildRun.project_id == project_id,
                BuildRun.status.in_(ACTIVE_BUILD_RUN_STATUSES),
            )
            .with_for_update()
        )
        if active is not None:
            raise ConflictException("项目已有构建任务正在运行")
        source = db.scalar(
            select(BuildRun).where(
                BuildRun.project_id == project_id,
                BuildRun.run_id == source_run_id,
            )
        )
        if source is None or source.status not in {
            BuildRunStatus.SUCCEEDED.value,
            BuildRunStatus.FAILED.value,
            BuildRunStatus.CANCELLED.value,
        }:
            raise ConflictException("只能从已结束的 BuildRun 创建修订")
        approved = _approved_baseline(db, project_id)
        code = _latest_code(db, project_id, source_run_id)
        if kind == "implementation_repair" and code is None:
            raise ConflictException("上一版没有可修复的代码成果")
        run = BuildRun(
            project_id=project_id,
            run_id=f"run_{uuid4().hex}",
            status=BuildRunStatus.QUEUED.value,
            active_slot=1,
        )
        db.add(run)
        db.flush()
        db.add(
            RunRevision(
                revision_id=f"rev_{uuid4().hex}",
                project_id=project_id,
                source_run_id=source_run_id,
                target_run_id=run.run_id,
                cause_message_id=cause_message_id,
                kind=kind,
                baseline_app_spec_item_id=approved.item_id,
                baseline_code_item_id=code.item_id if code else None,
            )
        )
        db.commit()
        db.refresh(run)
        return run
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("修订运行保存冲突，请重试") from exc
    except Exception:
        db.rollback()
        raise


def create_product_revision_plan(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
) -> Plan:
    revision = get_revision_for_target(db, project_id, run_id)
    if revision is None or revision.kind != "product_change":
        raise ConflictException("当前 BuildRun 不是产品修订")
    return plan_service.save_plan(
        db,
        user,
        project_id,
        run_id,
        PlanCreate(
            version=1,
            cause_message_id=revision.cause_message_id,
            tasks=[
                TaskCreate(
                    task_key="requirements",
                    recipient=TaskRecipient.PRODUCT_MANAGER,
                    title="根据用户反馈修订产品意图",
                    instructions=PRODUCT_REVISION_INSTRUCTIONS,
                    expected_output_type=ConfigurationItemType.APP_SPEC,
                    input_configuration_item_ids=[revision.baseline_app_spec_item_id],
                )
            ],
        ),
    )
