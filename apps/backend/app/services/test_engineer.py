"""Test Engineer task lifecycle and immutable quality-report publication."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.prompts.test_engineer import TEST_ENGINEER_PROMPT_VERSION
from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.core.settings import settings
from app.models.build_run import BuildRunStage, BuildRunStatus
from app.models.configuration_item import ConfigurationItem, ConfigurationItemType
from app.models.plan import Plan, PlanStatus
from app.models.project import Project, ProjectStatus
from app.models.task import Task, TaskRecipient, TaskStatus
from app.models.task_execution import TaskExecution
from app.models.task_result import TaskResult
from app.models.user import User
from app.schemas.app_spec import AppSpec
from app.schemas.code_artifact import CodeArtifact
from app.schemas.configuration_item import ConfigurationItemRegistration
from app.schemas.system_design import SystemDesign
from app.schemas.test_report import QualityConclusion, TestReport
from app.services import build_run as build_run_service
from app.services import configuration_manager, task_execution
from app.services import plan as plan_service
from app.services import task as task_service
from app.services.engineering.handoff import (
    ENGINEERING_TASK_KEY,
    QUALITY_TASK_KEY,
    load_engineering_source,
)

TEST_REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class TestInputs:
    code_item: ConfigurationItem
    code_artifact: CodeArtifact
    code_task: Task
    code_plan: Plan
    app_spec: AppSpec
    system_design: SystemDesign | None


def _load_task(
    db: Session,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    lock: bool = False,
) -> tuple[Task, Plan]:
    statement = (
        select(Task, Plan)
        .join(Plan, Task.plan_id == Plan.plan_id)
        .where(
            Plan.project_id == project_id,
            Plan.build_run_id == run_id,
            Task.task_id == task_id,
        )
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    row = db.execute(statement).one_or_none()
    if row is None:
        raise NotFoundException("Test Engineer 任务不存在或不属于指定构建")
    return row[0], row[1]


def _validate_task(task: Task) -> str:
    if (
        task.task_key != QUALITY_TASK_KEY
        or task.recipient != TaskRecipient.TEST_ENGINEER.value
        or task.expected_output_type != ConfigurationItemType.TEST_REPORT.value
        or len(task.input_configuration_item_ids) != 1
        or task.depends_on_task_ids
    ):
        raise BusinessException("Test Engineer 任务定义不符合质量验证约定")
    return task.input_configuration_item_ids[0]


def load_test_inputs(
    db: Session,
    project_id: int,
    run_id: str,
    code_item_id: str,
    *,
    lock: bool = False,
) -> TestInputs:
    statement = (
        select(ConfigurationItem, Task, Plan)
        .join(TaskResult, TaskResult.configuration_item_id == ConfigurationItem.item_id)
        .join(Task, Task.task_id == TaskResult.task_id)
        .join(Plan, Plan.plan_id == Task.plan_id)
        .where(
            ConfigurationItem.item_id == code_item_id,
            ConfigurationItem.project_id == project_id,
            ConfigurationItem.producer_run_id == run_id,
            Plan.project_id == project_id,
            Plan.build_run_id == run_id,
        )
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    row = db.execute(statement).one_or_none()
    if row is None:
        raise NotFoundException("code 成果不存在或不属于当前构建")
    code_item, code_task, code_plan = row
    if (
        (code_item.semantic_type, code_item.state) != (ConfigurationItemType.CODE.value, "usable")
        or code_task.task_key != ENGINEERING_TASK_KEY
        or code_task.recipient != TaskRecipient.CODE_ENGINEER.value
        or code_task.expected_output_type != ConfigurationItemType.CODE.value
        or code_task.status != TaskStatus.SUCCEEDED.value
        or code_plan.status != PlanStatus.SUCCEEDED.value
        or len(code_item.upstream_item_ids) not in {1, 2}
    ):
        raise ConflictException("只能验证已完成且可用的准确 code 成果")
    try:
        artifact = CodeArtifact.model_validate(code_item.payload)
    except ValidationError as exc:
        raise ConflictException("code 成果身份不符合要求") from exc
    source = load_engineering_source(
        db,
        project_id,
        run_id,
        code_item.upstream_item_ids[0],
        lock=lock,
    )
    return TestInputs(
        code_item=code_item,
        code_artifact=artifact,
        code_task=code_task,
        code_plan=code_plan,
        app_spec=source.app_spec,
        system_design=source.system_design,
    )


def claim_test_engineer_task(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    recovery_execution_id: str | None = None,
) -> tuple[Task, TaskExecution]:
    """Claim the exact quality task and create a fenced execution attempt."""

    try:
        run = task_execution.lock_run(db, user, project_id, run_id)
        task, plan = _load_task(db, project_id, run_id, task_id, lock=True)
        code_item_id = _validate_task(task)
        load_test_inputs(db, project_id, run_id, code_item_id, lock=True)
        build_run_service.require_active_for_stages(
            run,
            {BuildRunStage.DEVELOPER, BuildRunStage.QA},
        )
        if task.status == TaskStatus.PENDING.value:
            build_run_service.stage_running(
                db,
                run,
                stage=BuildRunStage.QA,
                allowed_running_stages={BuildRunStage.DEVELOPER, BuildRunStage.QA},
            )
            plan_service.stage_running(db, plan)
            task_service.stage_running(db, task)
            db.commit()
            db.refresh(task)
        elif not (
            task.status == TaskStatus.RUNNING.value
            and plan.status == PlanStatus.RUNNING.value
            and run.stage == BuildRunStage.QA.value
        ):
            raise ConflictException("Test Engineer 任务已结束或状态不一致")
    except Exception:
        db.rollback()
        raise

    execution = task_execution.start_execution(
        db,
        user,
        project_id,
        run_id,
        task_id,
        recovery_execution_id=recovery_execution_id,
    )
    return task, execution


def complete_test_engineer_task(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    execution_id: str,
    report: TestReport,
) -> ConfigurationItem:
    """Publish one test_report and close the BuildRun from its quality conclusion."""

    report = TestReport.model_validate(report.model_dump())
    payload = report.model_dump(mode="json")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    try:
        run = task_execution.lock_run(db, user, project_id, run_id)
        task, plan = _load_task(db, project_id, run_id, task_id, lock=True)
        code_item_id = _validate_task(task)
        inputs = load_test_inputs(db, project_id, run_id, code_item_id, lock=True)
        if run.stage != BuildRunStage.QA.value:
            raise ConflictException("当前构建不在 Test Engineer 阶段")
        if (report.code_item_id, report.source_hash) != (
            inputs.code_item.item_id,
            inputs.code_artifact.source_hash,
        ):
            raise BusinessException("测试报告引用的不是任务指定的准确代码结果")
        saved = db.get(TaskResult, task_id)
        if saved is not None:
            if saved.result_hash != result_hash:
                raise ConflictException("Test Engineer 任务已登记不同结果，不能覆盖")
            item = db.scalar(
                select(ConfigurationItem).where(
                    ConfigurationItem.item_id == saved.configuration_item_id
                )
            )
            if item is None:
                raise ConflictException("Test Engineer 任务产出关联异常")
            db.commit()
            return item
        if task.status != TaskStatus.RUNNING.value or plan.status != PlanStatus.RUNNING.value:
            raise ConflictException("Test Engineer 任务没有提交资格")
        execution = task_execution.require_execution(db, task_id, execution_id, lock=True)
        assert execution is not None
        item = configuration_manager.stage_configuration_item(
            db,
            project_id=project_id,
            producer_run_id=run_id,
            submission=ConfigurationItemRegistration(
                semantic_type=ConfigurationItemType.TEST_REPORT,
                schema_version=TEST_REPORT_SCHEMA_VERSION,
                payload=payload,
                upstream_item_ids=[code_item_id],
            ),
        )
        db.add(
            TaskResult(
                task_id=task_id,
                configuration_item_id=item.item_id,
                result_hash=result_hash,
                source_message_ids=[plan.cause_message_id],
                context_truncated=False,
                model=settings.deepseek_model,
                prompt_version=TEST_ENGINEER_PROMPT_VERSION,
            )
        )
        task_service.stage_succeeded(task)
        execution.status = "succeeded"
        execution.active_slot = None
        execution.finished_at = task_execution.utc_now()
        plan_service.stage_succeeded_if_tasks_complete(db, plan)
        if report.quality_conclusion == QualityConclusion.PASSED:
            project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
            assert project is not None
            project.status = ProjectStatus.AVAILABLE.value
            run.status = BuildRunStatus.SUCCEEDED.value
            run.error = None
            run.active_slot = None
        elif report.quality_conclusion == QualityConclusion.FAILED:
            # A failed report is structured feedback, not a terminal build state.
            # Leader will either create a bounded repair assignment or close the
            # run after the configured number of independent QA cycles.
            run.error = report.summary[:500]
        else:
            # Missing infrastructure or evidence is not a code defect. Sending it
            # back to Code Engineer would burn repair rounds without changing the
            # observation, so close with the actionable blocked reason.
            run.status = BuildRunStatus.FAILED.value
            run.active_slot = None
            run.error = report.summary[:500]
        db.commit()
        db.refresh(item)
        return item
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("测试报告保存冲突，请重试") from exc
    except Exception:
        db.rollback()
        raise
