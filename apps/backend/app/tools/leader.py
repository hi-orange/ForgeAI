"""Bounded planning and dispatch tools for Leader."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.core.exceptions import BusinessException
from app.models.configuration_item import ConfigurationItemType
from app.models.project_message_classification import ProjectMessageCategory
from app.models.task import TaskRecipient, TaskStatus
from app.schemas.agent_action import ToolCall, ToolDefinition, ToolExecutionResult
from app.schemas.leader import (
    LeaderContext,
    LeaderIntentDecision,
    LeaderOutcome,
)
from app.schemas.plan import PlanCreate
from app.schemas.project_message_classification import ProjectMessageClassificationDecision

_PLAN_SCHEMA = PlanCreate.model_json_schema()
_INTENT_SCHEMA = LeaderIntentDecision.model_json_schema()
_MESSAGE_CLASSIFICATION_SCHEMA = ProjectMessageClassificationDecision.model_json_schema()

MESSAGE_CLASSIFICATION_TOOL = ToolDefinition(
    name="record_message_classification",
    description="提交本次目标用户消息的唯一业务分类和一句可审计摘要。",
    parameters={
        "type": "object",
        "properties": {"decision": _MESSAGE_CLASSIFICATION_SCHEMA},
        "required": ["decision"],
        "additionalProperties": False,
    },
)

LEADER_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="read_project_context",
        description="读取本轮冻结的项目、目标消息、历史消息、运行、计划和任务摘要。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolDefinition(
        name="classify_intent",
        description="记录用户意图、业务优先级和简短判断摘要。",
        parameters={
            "type": "object",
            "properties": {"intent": _INTENT_SCHEMA},
            "required": ["intent"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="read_plan",
        description="读取指定计划；省略 plan_id 时读取最新计划和当前草稿。",
        parameters={
            "type": "object",
            "properties": {"plan_id": {"type": "string", "maxLength": 40}},
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="create_plan",
        description="创建一个完整的新版本计划草稿，包含角色、任务依赖和准确输入成果。",
        parameters={
            "type": "object",
            "properties": {"plan": _PLAN_SCHEMA},
            "required": ["plan"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="update_plan",
        description="更新本轮尚未提交的计划草稿；不能覆盖已有持久化计划。",
        parameters={
            "type": "object",
            "properties": {"plan": _PLAN_SCHEMA},
            "required": ["plan"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="dispatch_task",
        description="把草稿或已有计划中依赖已满足的一项任务分派给其指定角色。",
        parameters={
            "type": "object",
            "properties": {"task_key": {"type": "string", "minLength": 1, "maxLength": 64}},
            "required": ["task_key"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="read_task_result",
        description="读取准确 task_id 的状态和已登记结果；不按角色猜测最新结果。",
        parameters={
            "type": "object",
            "properties": {"task_id": {"type": "string", "minLength": 1, "maxLength": 40}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="request_user_input",
        description="仅在缺少不可推断的必要信息时提出一个问题，并结束本轮管理。",
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "minLength": 1, "maxLength": 2000},
                "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            "required": ["question", "summary"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="finish_turn",
        description="结束本轮管理，明确继续分派、开始新计划、取消或收尾。",
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["dispatch", "continue", "finish", "cancel"],
                },
                "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            "required": ["action", "summary"],
            "additionalProperties": False,
        },
    ),
]


@dataclass(slots=True)
class LeaderToolState:
    context: LeaderContext
    intent: LeaderIntentDecision | None = None
    draft: PlanCreate | None = None
    dispatched_task_keys: list[str] = field(default_factory=list)


_ROLE_OUTPUT = {
    TaskRecipient.PRODUCT_MANAGER: ConfigurationItemType.APP_SPEC,
    TaskRecipient.ARCHITECT: ConfigurationItemType.SYSTEM_DESIGN,
    TaskRecipient.CODE_ENGINEER: ConfigurationItemType.CODE,
    TaskRecipient.TEST_ENGINEER: ConfigurationItemType.TEST_REPORT,
}


def _tool_result(
    call: ToolCall,
    *,
    summary: str,
    data: dict[str, Any] | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_call_id=call.id,
        name=call.name,
        ok=True,
        summary=summary,
        data=data or {},
        arguments=dict(call.arguments),
    )


def execute_message_classification_tool(
    call: ToolCall,
) -> tuple[ToolExecutionResult, ProjectMessageClassificationDecision]:
    try:
        decision = ProjectMessageClassificationDecision.model_validate(
            call.arguments.get("decision")
        )
    except ValidationError as exc:
        raise BusinessException("Leader 消息分类返回格式异常") from exc
    return (
        _tool_result(
            call,
            summary="已记录用户消息分类",
            data={"decision": decision.model_dump(mode="json")},
        ),
        decision,
    )


def _validate_plan(state: LeaderToolState, value: Any) -> PlanCreate:
    try:
        plan = PlanCreate.model_validate(value)
    except ValidationError as exc:
        raise BusinessException("Leader 计划缺少有效任务、角色或依赖关系") from exc
    if plan.cause_message_id != state.context.target_message.id:
        raise BusinessException("计划必须由本轮目标用户消息触发")
    for task in plan.tasks:
        if _ROLE_OUTPUT[task.recipient] != task.expected_output_type:
            raise BusinessException(
                f"{task.recipient.value} 不能产出 {task.expected_output_type.value}"
            )
    return plan


def _next_plan_version(state: LeaderToolState) -> int:
    return max((plan.version for plan in state.context.plans), default=0) + 1


def _find_snapshot_task(state: LeaderToolState, task_key: str):
    for plan in reversed(state.context.plans):
        for task in plan.tasks:
            if task.task_key == task_key:
                return task, plan
    return None


def _dispatch_task(state: LeaderToolState, task_key: str) -> dict[str, Any]:
    if task_key in state.dispatched_task_keys:
        raise BusinessException("该任务已在本轮分派")
    if state.draft is not None:
        task_by_key = {task.task_key: task for task in state.draft.tasks}
        task = task_by_key.get(task_key)
        if task is not None:
            completed_keys = {
                snapshot.task_key
                for plan in state.context.plans
                for snapshot in plan.tasks
                if snapshot.status == TaskStatus.SUCCEEDED
            }
            missing = set(task.depends_on_task_keys) - completed_keys
            if missing:
                raise BusinessException(f"任务依赖尚未完成：{', '.join(sorted(missing))}")
            state.dispatched_task_keys.append(task_key)
            return {
                "task_key": task.task_key,
                "recipient": task.recipient.value,
                "expected_output_type": task.expected_output_type.value,
            }
    found = _find_snapshot_task(state, task_key)
    if found is None:
        raise BusinessException("计划中不存在该任务")
    task, plan = found
    if task.status != TaskStatus.PENDING:
        raise BusinessException("只能分派 pending 任务")
    statuses = {candidate.task_id: candidate.status for candidate in plan.tasks}
    missing_ids = [
        task_id
        for task_id in task.depends_on_task_ids
        if statuses.get(task_id) != TaskStatus.SUCCEEDED
    ]
    if missing_ids:
        raise BusinessException("任务依赖尚未完成")
    state.dispatched_task_keys.append(task_key)
    return {
        "task_key": task.task_key,
        "recipient": task.recipient.value,
        "status": task.status.value,
    }


def _require_intent(state: LeaderToolState) -> LeaderIntentDecision:
    if state.intent is None:
        raise BusinessException("Leader 必须先判断意图和优先级")
    return state.intent


def execute_leader_tool(
    call: ToolCall,
    state: LeaderToolState,
) -> tuple[ToolExecutionResult, LeaderOutcome | None]:
    try:
        if call.name == "read_project_context":
            return (
                _tool_result(
                    call,
                    summary="已读取冻结项目上下文",
                    data={"context": state.context.model_dump(mode="json")},
                ),
                None,
            )
        if call.name == "classify_intent":
            try:
                state.intent = LeaderIntentDecision.model_validate(call.arguments.get("intent"))
            except ValidationError as exc:
                raise BusinessException("Leader 意图或优先级格式异常") from exc
            return (
                _tool_result(
                    call,
                    summary="已记录用户意图和优先级",
                    data={"intent": state.intent.model_dump(mode="json")},
                ),
                None,
            )
        if call.name == "read_plan":
            plan_id = str(call.arguments.get("plan_id") or "").strip()
            plans = state.context.plans
            selected = (
                next((plan for plan in plans if plan.plan_id == plan_id), None)
                if plan_id
                else (plans[-1] if plans else None)
            )
            if plan_id and selected is None:
                raise BusinessException("指定计划不存在")
            return (
                _tool_result(
                    call,
                    summary="已读取计划状态",
                    data={
                        "plan": selected.model_dump(mode="json") if selected else None,
                        "draft": state.draft.model_dump(mode="json") if state.draft else None,
                    },
                ),
                None,
            )
        if call.name == "create_plan":
            intent = _require_intent(state)
            if intent.category not in {
                ProjectMessageCategory.PRODUCT_CHANGE,
                ProjectMessageCategory.IMPLEMENTATION_REPAIR,
            }:
                raise BusinessException("当前用户意图不需要创建执行计划")
            if state.draft is not None:
                raise BusinessException("计划草稿已存在，请使用 update_plan")
            plan = _validate_plan(state, call.arguments.get("plan"))
            if plan.version != _next_plan_version(state):
                raise BusinessException("新计划必须使用下一个连续版本")
            state.draft = plan
            return (
                _tool_result(
                    call,
                    summary=f"已创建计划草稿 v{plan.version}",
                    data={"plan": plan.model_dump(mode="json")},
                ),
                None,
            )
        if call.name == "update_plan":
            if state.draft is None:
                raise BusinessException("尚无可更新的计划草稿")
            plan = _validate_plan(state, call.arguments.get("plan"))
            if (plan.version, plan.cause_message_id) != (
                state.draft.version,
                state.draft.cause_message_id,
            ):
                raise BusinessException("更新草稿不能改变计划版本或触发消息")
            state.draft = plan
            state.dispatched_task_keys = [
                key
                for key in state.dispatched_task_keys
                if any(t.task_key == key for t in plan.tasks)
            ]
            return (
                _tool_result(
                    call,
                    summary="已更新计划草稿",
                    data={"plan": plan.model_dump(mode="json")},
                ),
                None,
            )
        if call.name == "dispatch_task":
            task_key = str(call.arguments.get("task_key") or "").strip()
            if not task_key:
                raise BusinessException("分派任务必须提供 task_key")
            assignment = _dispatch_task(state, task_key)
            return (
                _tool_result(call, summary=f"已分派任务：{task_key}", data=assignment),
                None,
            )
        if call.name == "read_task_result":
            task_id = str(call.arguments.get("task_id") or "").strip()
            task = next(
                (
                    task
                    for plan in state.context.plans
                    for task in plan.tasks
                    if task.task_id == task_id
                ),
                None,
            )
            if task is None:
                raise BusinessException("任务不存在于本轮冻结上下文")
            return (
                _tool_result(
                    call,
                    summary=f"已读取任务结果：{task.task_key}",
                    data={"task": task.model_dump(mode="json")},
                ),
                None,
            )
        if call.name == "request_user_input":
            intent = _require_intent(state)
            question = str(call.arguments.get("question") or "").strip()
            summary = str(call.arguments.get("summary") or "").strip()
            outcome = LeaderOutcome(
                intent=intent,
                action="wait_user",
                summary=summary,
                plan=state.draft,
                dispatched_task_keys=list(state.dispatched_task_keys),
                question=question,
            )
            return _tool_result(call, summary="需要用户补充必要信息"), outcome
        if call.name == "finish_turn":
            intent = _require_intent(state)
            action = str(call.arguments.get("action") or "").strip()
            summary = str(call.arguments.get("summary") or "").strip()
            if action == "cancel" and intent.category != ProjectMessageCategory.STOP:
                raise BusinessException("只有明确停止意图才能取消计划")
            if action in {"dispatch", "continue"} and intent.category in {
                ProjectMessageCategory.INQUIRY,
                ProjectMessageCategory.STOP,
            }:
                raise BusinessException("当前用户意图不能继续分派任务")
            if action == "finish" and state.context.plans:
                unfinished = [
                    task.task_key
                    for task in state.context.plans[-1].tasks
                    if task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}
                ]
                if unfinished:
                    raise BusinessException("仍有未结束任务，不能收尾")
            outcome = LeaderOutcome.model_validate(
                {
                    "intent": intent,
                    "action": action,
                    "summary": summary,
                    "plan": state.draft,
                    "dispatched_task_keys": list(state.dispatched_task_keys),
                }
            )
            return _tool_result(call, summary="Leader 已结束本轮管理"), outcome
        raise BusinessException(f"Leader 无权使用工具：{call.name}")
    except (BusinessException, ValidationError, ValueError) as exc:
        return (
            ToolExecutionResult(
                tool_call_id=call.id,
                name=call.name,
                ok=False,
                error_code="TOOL_REJECTED",
                summary=str(exc),
                arguments=dict(call.arguments),
            ),
            None,
        )
