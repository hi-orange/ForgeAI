import logging
from collections.abc import Callable
from typing import Literal, NotRequired, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
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
from app.services import configuration_manager, task_execution
from app.services import plan as plan_service
from app.services import product_manager as product_manager_service
from app.services import project_manager as project_manager_service
from app.services import task as task_service
from app.services.design_handoff import APPROVAL_VERSION, find_pending_engineering_task
from app.services.requirement_inputs import read_app_spec

logger = logging.getLogger("forgeai")


class _WorkflowInputState(TypedDict):
    user_id: int
    project_id: int
    build_run_id: str
    cause_message_id: int
    recovery_execution_id: NotRequired[str | None]


class _WorkflowOutputState(TypedDict):
    project_id: int
    build_run_id: str
    cause_message_id: int
    plan_id: str
    task_id: str
    configuration_item_id: str
    outcome: str
    open_questions: list[str]
    design_plan_id: NotRequired[str]
    design_task_id: NotRequired[str]


class _WorkflowState(_WorkflowInputState, total=False):
    approved: bool
    design_plan_id: str
    design_task_id: str
    plan_id: NotRequired[str]
    task_id: NotRequired[str]
    task_status: NotRequired[str]
    product_manager_result: NotRequired[dict[str, object]]
    app_spec: NotRequired[dict[str, object]]
    configuration_item_id: NotRequired[str]
    outcome: NotRequired[str]
    open_questions: NotRequired[list[str]]
    execution_id: str
    saved_draft: dict[str, object] | None


SessionFactory = Callable[[], Session]
TaskRoute = Literal["claim_task", "start_execution", "load_saved_app_spec"]
QuestionRoute = Literal["needs_user_input", "awaiting_approval", "ready_for_design"]


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
        raise BusinessException("计划中的任务不符合 ProductManager 工作流要求")
    return task


def _validated_app_spec(payload: object) -> AppSpec:
    try:
        return AppSpec.model_validate(payload)
    except ValidationError as exc:
        raise ConflictException("已保存的 app_spec 内容损坏，工作流不能继续") from exc


class _ProductManagerNodes:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def ensure_plan(self, state: _WorkflowState) -> dict[str, object]:
        """创建初始计划或重放本轮补充计划，找到其中唯一的需求任务。"""

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
                plan = project_manager_service.create_initial_plan(
                    db, user, state["project_id"], state["build_run_id"], state["cause_message_id"]
                )
            tasks = task_service.list_user_plan_tasks(db, user, state["project_id"], plan.plan_id)
            task = _require_requirements_task(tasks)
            return {
                "plan_id": plan.plan_id,
                "task_id": task.task_id,
                "task_status": task.status,
            }

    @staticmethod
    def route_task(state: _WorkflowState) -> TaskRoute:
        status = state.get("task_status")
        if status == TaskStatus.PENDING.value:
            return "claim_task"
        if status == TaskStatus.RUNNING.value:
            return "start_execution"
        if status == TaskStatus.SUCCEEDED.value:
            return "load_saved_app_spec"
        raise ConflictException("需求任务已失败或取消，当前工作流不能继续")

    def claim_task(self, state: _WorkflowState) -> dict[str, object]:
        """只领取 Plan 明确指定的任务，不在图中另选最新任务。"""

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

    def generate_app_spec(self, state: _WorkflowState) -> dict[str, object]:
        """调用 ProductManager；图中只保留可序列化、可再次校验的草稿。"""

        # 先在短会话中取得并脱离用户对象。generate_task_app_spec 自己会使用两个
        # 独立的只读会话，因此等待模型时这里不会额外占着业务查询事务。
        with self._session_factory() as identity_db:
            user = _get_user(identity_db, state["user_id"])
            identity_db.expunge(user)
        try:
            with self._session_factory() as db:
                saved_draft = state.get("saved_draft")
                if saved_draft is not None:
                    draft = dict(saved_draft)
                    if draft.get("schema_version") == 1:
                        # Only an unpublished draft is upgraded; saved artifacts stay immutable.
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

    def start_execution(self, state: _WorkflowState) -> dict[str, object]:
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

    def _fail_execution(self, state: _WorkflowState) -> None:
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

    def complete_app_spec(self, state: _WorkflowState) -> dict[str, object]:
        """通过现有收尾服务原子发布成果并完成 Task / Plan。"""

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

    def load_saved_app_spec(self, state: _WorkflowState) -> dict[str, object]:
        """从 SQL 恢复已完成任务，绝不依赖图内残留的模型草稿。"""

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
            if latest_plan_id != plan.plan_id:
                design_task = find_pending_engineering_task(db, plan, item.item_id)
                if (
                    app_spec.open_questions
                    or design_task is None
                    or design_task.plan_id != latest_plan_id
                ):
                    raise ConflictException("需求已进入后续计划，请刷新进度")
            return {
                "configuration_item_id": item.item_id,
                "app_spec": app_spec.model_dump(mode="json"),
                "approved": result.prompt_version == APPROVAL_VERSION,
            }

    @staticmethod
    def route_questions(state: _WorkflowState) -> QuestionRoute:
        app_spec = _validated_app_spec(state.get("app_spec"))
        if state.get("approved"):
            return "ready_for_design"
        return "awaiting_approval" if app_spec.features else "needs_user_input"

    @staticmethod
    def awaiting_approval(state: _WorkflowState) -> dict[str, object]:
        app_spec = _validated_app_spec(state.get("app_spec"))
        return {
            "outcome": ProductManagerWorkflowOutcome.AWAITING_APPROVAL,
            "open_questions": list(app_spec.open_questions),
        }

    @staticmethod
    def needs_user_input(state: _WorkflowState) -> dict[str, object]:
        app_spec = _validated_app_spec(state.get("app_spec"))
        return {
            "outcome": ProductManagerWorkflowOutcome.NEEDS_USER_INPUT,
            "open_questions": list(app_spec.open_questions),
        }

    @staticmethod
    def ready_for_design(state: _WorkflowState) -> dict[str, object]:
        _validated_app_spec(state.get("app_spec"))
        return {
            "outcome": ProductManagerWorkflowOutcome.READY_FOR_DESIGN,
            "open_questions": [],
        }

    def assign_design_task(self, state: _WorkflowState) -> dict[str, object]:
        with self._session_factory() as db:
            task = project_manager_service.create_engineering_delivery_task(
                db,
                _get_user(db, state["user_id"]),
                state["project_id"],
                state["build_run_id"],
                state["configuration_item_id"],
            )
            return {"design_plan_id": task.plan_id, "design_task_id": task.task_id}


def build_product_manager_workflow(
    session_factory: SessionFactory,
) -> CompiledStateGraph[
    _WorkflowState,
    None,
    _WorkflowInputState,
    _WorkflowOutputState,
]:
    """组装产品需求阶段的 LangGraph；业务事实始终由 SQL 服务维护。"""

    nodes = _ProductManagerNodes(session_factory)
    builder = StateGraph(
        _WorkflowState,
        input_schema=_WorkflowInputState,
        output_schema=_WorkflowOutputState,
    )
    builder.add_node("ensure_plan", nodes.ensure_plan)
    builder.add_node("claim_task", nodes.claim_task)
    builder.add_node("start_execution", nodes.start_execution)
    builder.add_node("generate_app_spec", nodes.generate_app_spec)
    builder.add_node("complete_app_spec", nodes.complete_app_spec)
    builder.add_node("load_saved_app_spec", nodes.load_saved_app_spec)
    builder.add_node("needs_user_input", nodes.needs_user_input)
    builder.add_node("awaiting_approval", nodes.awaiting_approval)
    builder.add_node("ready_for_design", nodes.ready_for_design)
    builder.add_node("assign_design_task", nodes.assign_design_task)

    builder.add_edge(START, "ensure_plan")
    builder.add_conditional_edges(
        "ensure_plan",
        nodes.route_task,
        {
            "claim_task": "claim_task",
            "start_execution": "start_execution",
            "load_saved_app_spec": "load_saved_app_spec",
        },
    )
    builder.add_edge("claim_task", "start_execution")
    builder.add_edge("start_execution", "generate_app_spec")
    builder.add_edge("generate_app_spec", "complete_app_spec")
    builder.add_edge("complete_app_spec", "load_saved_app_spec")
    builder.add_conditional_edges(
        "load_saved_app_spec",
        nodes.route_questions,
        {
            "needs_user_input": "needs_user_input",
            "awaiting_approval": "awaiting_approval",
            "ready_for_design": "ready_for_design",
        },
    )
    builder.add_edge("needs_user_input", END)
    builder.add_edge("awaiting_approval", END)
    builder.add_edge("ready_for_design", "assign_design_task")
    builder.add_edge("assign_design_task", END)
    return builder.compile(name="forgeai_product_manager")


def run_product_manager_workflow(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    message_id: int,
    *,
    recovery_execution_id: str | None = None,
) -> ProductManagerWorkflowResult:
    """执行或从 SQL 业务边界恢复本轮 ProductManager 工作流。"""

    try:
        payload = ProductManagerWorkflowInput(
            user_id=user.id,
            project_id=project_id,
            build_run_id=run_id,
            cause_message_id=message_id,
            recovery_execution_id=recovery_execution_id,
        )
    except ValidationError as exc:
        raise BusinessException("ProductManager 工作流输入不符合要求") from exc

    if db.new or db.dirty or db.deleted:
        raise BusinessException("请先保存调用方待提交的修改，再启动工作流")
    db.rollback()
    factory = sessionmaker(
        bind=db.get_bind(),
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    graph = build_product_manager_workflow(cast(SessionFactory, factory))
    graph_input: _WorkflowInputState = {
        "user_id": payload.user_id,
        "project_id": payload.project_id,
        "build_run_id": payload.build_run_id,
        "cause_message_id": payload.cause_message_id,
        "recovery_execution_id": payload.recovery_execution_id,
    }
    raw_result = graph.invoke(graph_input)
    try:
        result = ProductManagerWorkflowResult.model_validate(raw_result)
    except ValidationError as exc:
        raise BusinessException("ProductManager 工作流没有产生完整结果") from exc
    return result
