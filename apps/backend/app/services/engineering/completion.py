"""Publish an exact generated workspace as the Code Engineer task result."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.prompts.code_engineer import CODE_ENGINEER_PROMPT_VERSION
from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.core.settings import settings
from app.generation.workspace import default_workspace_path
from app.models.build_run import BuildRunStage
from app.models.configuration_item import ConfigurationItem, ConfigurationItemType
from app.models.plan import Plan, PlanStatus
from app.models.task import Task, TaskRecipient, TaskStatus
from app.models.task_result import TaskResult
from app.models.user import User
from app.schemas.code_artifact import (
    CODE_ARTIFACT_SCHEMA_VERSION,
    CodeArtifact,
    SourceFileIdentity,
)
from app.schemas.configuration_item import ConfigurationItemRegistration
from app.services import configuration_manager, task_execution
from app.services import plan as plan_service
from app.services import task as task_service
from app.services.engineering.claim import read_frozen_input_snapshot
from app.services.engineering.handoff import ENGINEERING_TASK_KEY, load_engineering_source
from app.tools.checks import source_snapshot


def _source_manifest(payload: bytes) -> list[SourceFileIdentity]:
    try:
        files = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BusinessException("源码快照格式异常") from exc
    if not isinstance(files, dict) or not files:
        raise BusinessException("源码快照为空，不能发布 code 成果")
    manifest: list[SourceFileIdentity] = []
    for path, content in files.items():
        if not isinstance(path, str) or not isinstance(content, str):
            raise BusinessException("源码快照格式异常")
        raw = content.encode("utf-8")
        manifest.append(
            SourceFileIdentity(
                path=path,
                size_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    return manifest


def _load_task(
    db: Session,
    project_id: int,
    run_id: str,
    task_id: str,
) -> tuple[Task, Plan]:
    row = db.execute(
        select(Task, Plan)
        .join(Plan, Task.plan_id == Plan.plan_id)
        .where(
            Plan.project_id == project_id,
            Plan.build_run_id == run_id,
            Task.task_id == task_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise NotFoundException("Code Engineer 任务不存在或不属于指定构建")
    return row[0], row[1]


def _result_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def complete_code_engineer_task(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    execution_id: str,
    *,
    workspace_root: Path | None = None,
) -> ConfigurationItem:
    """Atomically publish code identity and finish the exact generated attempt."""

    root = workspace_root or default_workspace_path(settings.runtime_data_root, project_id, run_id)
    snapshot_bytes, source_hash = source_snapshot(root)
    try:
        run = task_execution.lock_run(db, user, project_id, run_id)
        task, plan = _load_task(db, project_id, run_id, task_id)
        if (
            task.task_key != ENGINEERING_TASK_KEY
            or task.recipient != TaskRecipient.CODE_ENGINEER.value
            or task.expected_output_type != ConfigurationItemType.CODE.value
            or len(task.input_configuration_item_ids) != 1
            or task.depends_on_task_ids
        ):
            raise BusinessException("Code Engineer 任务定义不符合代码交付约定")
        if run.stage != BuildRunStage.DEVELOPER.value:
            raise ConflictException("当前构建不在 Code Engineer 阶段")
        load_engineering_source(
            db,
            project_id,
            run_id,
            task.input_configuration_item_ids[0],
            lock=True,
        )
        saved = db.get(TaskResult, task_id)
        if saved is not None:
            item = db.scalar(
                select(ConfigurationItem).where(
                    ConfigurationItem.item_id == saved.configuration_item_id
                )
            )
            if item is None or item.semantic_type != ConfigurationItemType.CODE.value:
                raise ConflictException("Code Engineer 任务产出关联异常")
            if item.payload.get("source_hash") != source_hash:
                raise ConflictException("工作区源码已偏离已发布 code 成果")
            db.commit()
            return item
        if task.status != TaskStatus.RUNNING.value or plan.status != PlanStatus.RUNNING.value:
            raise ConflictException("Code Engineer 任务没有提交资格")
        execution = task_execution.require_execution(db, task_id, execution_id, lock=True)
        assert execution is not None
        frozen = read_frozen_input_snapshot(execution)
        checkpoint = frozen.get("checkpoint") if frozen else None
        if not isinstance(frozen, dict) or not isinstance(checkpoint, dict):
            raise ConflictException("工程执行缺少冻结输入或完成 checkpoint")
        if checkpoint.get("outcome") != "generated":
            raise ConflictException("工程执行尚未完成全部工作单元")
        if checkpoint.get("completed_source_hash") != source_hash:
            raise ConflictException("源码在最终检查后发生变化，不能发布 code 成果")
        artifact = CodeArtifact(
            source_hash=source_hash,
            workspace_key=str(frozen.get("workspace_key") or run_id),
            template_version=str(frozen.get("template_version") or "unknown"),
            tool_strategy_version=str(frozen.get("tool_strategy_version") or "unknown"),
            files=_source_manifest(snapshot_bytes),
            modified_files=[str(path) for path in checkpoint.get("modified_files") or []],
            base_revision_id=(
                str(frozen["base_revision_id"]) if frozen.get("base_revision_id") else None
            ),
        )
        payload = artifact.model_dump(mode="json")
        item = configuration_manager.stage_configuration_item(
            db,
            project_id=project_id,
            producer_run_id=run_id,
            submission=ConfigurationItemRegistration(
                semantic_type=ConfigurationItemType.CODE,
                schema_version=CODE_ARTIFACT_SCHEMA_VERSION,
                payload=payload,
                upstream_item_ids=list(task.input_configuration_item_ids),
            ),
        )
        db.add(
            TaskResult(
                task_id=task_id,
                configuration_item_id=item.item_id,
                result_hash=_result_hash(payload),
                source_message_ids=[plan.cause_message_id],
                context_truncated=False,
                model=settings.deepseek_model,
                prompt_version=CODE_ENGINEER_PROMPT_VERSION,
            )
        )
        task_service.stage_succeeded(task)
        execution.status = "succeeded"
        execution.active_slot = None
        execution.finished_at = task_execution.utc_now()
        plan_service.stage_succeeded_if_tasks_complete(db, plan)
        db.commit()
        db.refresh(item)
        return item
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("代码成果保存冲突，请重试") from exc
    except Exception:
        db.rollback()
        raise
