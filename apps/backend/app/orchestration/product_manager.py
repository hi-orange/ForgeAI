"""Product-manager requirements workflow as plain sequential steps.

Business truth lives in SQL (Plan / Task / Execution / ConfigurationItem).
This module only chooses the resume entry from task status, runs the PM
generate/publish path when needed, then maps the saved app_spec to an outcome.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.models.build_run import BuildRunStage, BuildRunStatus
from app.models.configuration_item import ConfigurationItemState, ConfigurationItemType
from app.models.plan import Plan, PlanStatus
from app.models.requirement_clarification import RequirementClarification
from app.models.task import Task, TaskRecipient, TaskStatus
from app.models.user import User
from app.schemas.app_spec import AppSpec
from app.schemas.product_manager import ProductManagerResult
from app.schemas.product_manager_workflow import (
    ProductManagerWorkflowInput,
    ProductManagerWorkflowOutcome,
    ProductManagerWorkflowResult,
)
from app.services import build_run as build_run_service
from app.services import configuration_manager, engineering, task_execution
from app.services import leader as leader_service
from app.services import plan as plan_service
from app.services import product_manager as product_manager_service
from app.services import task as task_service
from app.services.app_spec import read_app_spec

logger = logging.getLogger("forgeai")

SessionFactory = Callable[[], Session]
WorkflowState = dict[str, Any]


def _get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundException("用户不存在")
    return user


def _require_requirements_task(tasks: list[Task]) -> Task:
    if len(tasks) != 1:
        raise ConflictException("需求计划必须且只能包含一个任务")
    task = tasks[0]
    if (
        task.task_key != "requirements"
        or task.recipient != TaskRecipient.PRODUCT_MANAGER.value
        or task.expected_output_type != ConfigurationItemType.APP_SPEC.value
        or len(task.input_configuration_item_ids) > 1
        or task.depends_on_task_ids
    ):
        raise BusinessException("计划中的任务不符合 Product Manager 工作流要求")
    return task


def _validated_app_spec(payload: object) -> AppSpec:
    try:
        return AppSpec.model_validate(payload)
    except ValidationError as exc:
        raise ConflictException("已保存的 app_spec 内容损坏，工作流不能继续") from exc


class _ProductManagerWorkflow:
    """Sequential PM steps; each method opens its own short DB session."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def ensure_plan(self, state: WorkflowState) -> dict[str, object]:
        """Find the clarification follow-up plan, or create the initial Plan v1."""

        with self._session_factory() as db:
            user = _get_user(db, state["user_id"])
            plan = db.scalar(
                select(Plan)
                .join(
                    RequirementClarification,
                    RequirementClarification.followup_plan_id == Plan.plan_id,
                )
                .where(
                    Plan.project_id == state["project_id"],
                    Plan.build_run_id == state["build_run_id"],
                    Plan.cause_message_id == state["cause_message_id"],
                )
            )
            if plan is None:
                plan = leader_service.create_initial_plan(
                    db, user, state["project_id"], state["build_run_id"], state["cause_message_id"]
                )
            tasks = task_service.list_user_plan_tasks(db, user, state["project_id"], plan.plan_id)
            task = _require_requirements_task(tasks)
            return {
                "plan_id": plan.plan_id,
                "task_id": task.task_id,
                "task_status": task.status,
            }

    def claim_task(self, state: WorkflowState) -> dict[str, object]:
        with self._session_factory() as db:
            user = _get_user(db, state["user_id"])
            task = task_service.claim_product_manager_task(
                db,
                user,
                state["project_id"],
                state["build_run_id"],
                state["task_id"],
            )
            return {"task_status": task.status}

    def start_execution(self, state: WorkflowState) -> dict[str, object]:
        with self._session_factory() as db:
            execution = task_execution.start_execution(
                db,
                _get_user(db, state["user_id"]),
                state["project_id"],
                state["build_run_id"],
                state["task_id"],
                recovery_execution_id=state.get("recovery_execution_id"),
            )
            return {"execution_id": execution.execution_id, "saved_draft": execution.draft}

    def _fail_execution(self, state: WorkflowState) -> None:
        try:
            with self._session_factory() as db:
                task_execution.fail_execution(
                    db,
                    state["task_id"],
                    state["execution_id"],
                    error="需求整理未完成，可重试恢复；已保存的需求版本不受影响。",
                )
        except Exception:
            logger.error("Could not mark execution failed: %s", state["execution_id"])

    def generate_app_spec(self, state: WorkflowState) -> dict[str, object]:
        with self._session_factory() as identity_db:
            user = _get_user(identity_db, state["user_id"])
            identity_db.expunge(user)
        try:
            with self._session_factory() as db:
                saved_draft = state.get("saved_draft")
                if saved_draft is not None:
                    draft = dict(saved_draft)
                    if draft.get("schema_version") == 1:
                        draft["schema_version"] = 2
                    result = ProductManagerResult.model_validate(draft)
                    result.execution_id = state["execution_id"]
                else:
                    result = product_manager_service.generate_task_app_spec(
                        db,
                        user,
                        state["project_id"],
                        state["build_run_id"],
                        state["task_id"],
                        execution_id=state["execution_id"],
                    )
                task_execution.lock_run(db, user, state["project_id"], state["build_run_id"])
                product_manager_service._load_running_task(
                    db, user, state["project_id"], state["build_run_id"], state["task_id"]
                )
                execution = task_execution.require_execution(
                    db, state["task_id"], state["execution_id"], lock=True
                )
                assert execution is not None
                execution.draft = result.model_dump(mode="json")
                db.commit()
        except Exception:
            self._fail_execution(state)
            raise
        return {"product_manager_result": result.model_dump(mode="json")}

    def complete_app_spec(self, state: WorkflowState) -> dict[str, object]:
        try:
            result = ProductManagerResult.model_validate(state["product_manager_result"])
        except (KeyError, ValidationError) as exc:
            self._fail_execution(state)
            raise BusinessException("工作流中的 ProductManager 草稿不符合要求") from exc
        try:
            with self._session_factory() as db:
                user = _get_user(db, state["user_id"])
                product_manager_service.complete_task_app_spec(
                    db,
                    user,
                    state["project_id"],
                    state["build_run_id"],
                    state["task_id"],
                    result,
                )
            return {}
        except Exception:
            self._fail_execution(state)
            raise

    def load_saved_app_spec(self, state: WorkflowState) -> dict[str, object]:
        """Reload the published app_spec from SQL only."""

        with self._session_factory() as db:
            user = _get_user(db, state["user_id"])
            run = build_run_service.get_user_build_run(
                db, user, state["project_id"], state["build_run_id"]
            )
            plan = plan_service.get_user_plan(db, user, state["project_id"], state["plan_id"])
            task = task_service.get_user_task(db, user, state["project_id"], state["task_id"])
            result = task_service.get_user_task_result(
                db, user, state["project_id"], state["task_id"]
            )
            item = configuration_manager.require_usable_configuration_item(
                db,
                project_id=state["project_id"],
                item_id=result.configuration_item_id,
            )
            if (
                run.status != BuildRunStatus.RUNNING.value
                or run.stage != BuildRunStage.PM.value
                or run.active_slot != 1
                or plan.build_run_id != state["build_run_id"]
                or plan.cause_message_id != state["cause_message_id"]
                or plan.status != PlanStatus.SUCCEEDED.value
                or task.plan_id != plan.plan_id
                or task.status != TaskStatus.SUCCEEDED.value
                or item.producer_run_id != state["build_run_id"]
                or item.semantic_type != ConfigurationItemType.APP_SPEC.value
                or item.state != ConfigurationItemState.USABLE.value
            ):
                raise ConflictException("已完成的需求任务与当前工作流状态不一致")
            app_spec = read_app_spec(item)
            latest_plan_id = db.scalar(
                select(Plan.plan_id)
                .where(Plan.build_run_id == state["build_run_id"])
                .order_by(Plan.version.desc())
                .limit(1)
            )
            approved = result.prompt_version == engineering.APPROVAL_VERSION
            if latest_plan_id != plan.plan_id:
                latest_tasks = list(
                    db.scalars(select(Task).where(Task.plan_id == latest_plan_id)).all()
                )
                downstream = latest_tasks[0] if len(latest_tasks) == 1 else None
                valid_leader_dispatch = downstream is not None and (
                    (
                        downstream.task_key == engineering.ARCHITECTURE_TASK_KEY
                        and downstream.recipient == TaskRecipient.ARCHITECT.value
                        and downstream.expected_output_type
                        == ConfigurationItemType.SYSTEM_DESIGN.value
                        and downstream.input_configuration_item_ids == [item.item_id]
                    )
                    or (
                        downstream.task_key == engineering.ENGINEERING_TASK_KEY
                        and downstream.recipient == TaskRecipient.CODE_ENGINEER.value
                        and downstream.expected_output_type == ConfigurationItemType.CODE.value
                        and downstream.input_configuration_item_ids == [item.item_id]
                    )
                )
                if not approved or not valid_leader_dispatch:
                    raise ConflictException("需求已进入后续计划，请刷新进度")
            return {
                "configuration_item_id": item.item_id,
                "app_spec": app_spec.model_dump(mode="json"),
                "approved": approved,
            }

    def run(self, state: WorkflowState) -> ProductManagerWorkflowResult:
        state.update(self.ensure_plan(state))

        task_status = state.get("task_status")
        if task_status == TaskStatus.PENDING.value:
            state.update(self.claim_task(state))
            state.update(self.start_execution(state))
            state.update(self.generate_app_spec(state))
            state.update(self.complete_app_spec(state))
        elif task_status == TaskStatus.RUNNING.value:
            state.update(self.start_execution(state))
            state.update(self.generate_app_spec(state))
            state.update(self.complete_app_spec(state))
        elif task_status == TaskStatus.SUCCEEDED.value:
            pass
        else:
            raise ConflictException("需求任务已失败或取消，当前工作流不能继续")

        state.update(self.load_saved_app_spec(state))
        app_spec = _validated_app_spec(state.get("app_spec"))

        if state.get("approved"):
            state["outcome"] = ProductManagerWorkflowOutcome.READY_FOR_DELIVERY
            state["open_questions"] = []
        elif app_spec.features:
            state["outcome"] = ProductManagerWorkflowOutcome.AWAITING_APPROVAL
            state["open_questions"] = list(app_spec.open_questions)
        else:
            state["outcome"] = ProductManagerWorkflowOutcome.NEEDS_USER_INPUT
            state["open_questions"] = list(app_spec.open_questions)

        try:
            return ProductManagerWorkflowResult(
                project_id=state["project_id"],
                build_run_id=state["build_run_id"],
                cause_message_id=state["cause_message_id"],
                plan_id=state["plan_id"],
                task_id=state["task_id"],
                configuration_item_id=state["configuration_item_id"],
                outcome=state["outcome"],
                open_questions=state["open_questions"],
            )
        except ValidationError as exc:
            raise BusinessException("Product Manager 工作流没有产生完整结果") from exc


def run_product_manager_workflow(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    message_id: int,
    *,
    recovery_execution_id: str | None = None,
) -> ProductManagerWorkflowResult:
    """执行或从 SQL 业务边界恢复本轮 Product Manager 工作流。"""

    try:
        payload = ProductManagerWorkflowInput(
            user_id=user.id,
            project_id=project_id,
            build_run_id=run_id,
            cause_message_id=message_id,
            recovery_execution_id=recovery_execution_id,
        )
    except ValidationError as exc:
        raise BusinessException("Product Manager 工作流输入不符合要求") from exc

    if db.new or db.dirty or db.deleted:
        raise BusinessException("请先保存调用方待提交的修改，再启动工作流")
    db.rollback()
    factory = sessionmaker(
        bind=db.get_bind(),
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    state: WorkflowState = {
        "user_id": payload.user_id,
        "project_id": payload.project_id,
        "build_run_id": payload.build_run_id,
        "cause_message_id": payload.cause_message_id,
        "recovery_execution_id": payload.recovery_execution_id,
    }
    return _ProductManagerWorkflow(factory).run(state)
