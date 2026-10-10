"""Bounded context and outcome contracts for Leader."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

from app.models.project_message_classification import ProjectMessageCategory
from app.models.task import TaskRecipient, TaskStatus
from app.schemas.plan import PlanCreate

LeaderText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]


class LeaderPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class LeaderMessageSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    sequence: int = Field(gt=0)
    sender: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1, max_length=8000)


class LeaderTaskSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=40)
    task_key: str = Field(min_length=1, max_length=64)
    recipient: TaskRecipient
    title: str = Field(min_length=1, max_length=200)
    status: TaskStatus
    depends_on_task_ids: list[str] = Field(default_factory=list, max_length=99)
    result: dict[str, JsonValue] | None = None


class LeaderPlanSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=40)
    version: int = Field(ge=1)
    status: Literal["pending", "running", "succeeded", "failed", "cancelled"]
    tasks: list[LeaderTaskSnapshot] = Field(default_factory=list, max_length=100)


class LeaderContext(BaseModel):
    """Frozen data for one management turn; callers select the exact message and run."""

    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(gt=0)
    project_name: str = Field(min_length=1, max_length=200)
    project_status: str = Field(min_length=1, max_length=32)
    run_id: str = Field(min_length=1, max_length=40)
    run_status: str = Field(min_length=1, max_length=32)
    target_message: LeaderMessageSnapshot
    recent_messages: list[LeaderMessageSnapshot] = Field(default_factory=list, max_length=20)
    plans: list[LeaderPlanSnapshot] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_frozen_context(self) -> Self:
        if self.target_message.sender != "user":
            raise ValueError("Leader 只能处理用户目标消息")
        message_keys = [(message.id, message.sequence) for message in self.recent_messages]
        if len(message_keys) != len(set(message_keys)):
            raise ValueError("历史消息不能重复")
        if any(
            message.sequence >= self.target_message.sequence for message in self.recent_messages
        ):
            raise ValueError("历史消息必须早于目标消息")
        if [message.sequence for message in self.recent_messages] != sorted(
            message.sequence for message in self.recent_messages
        ):
            raise ValueError("历史消息必须按顺序提供")
        versions = [plan.version for plan in self.plans]
        if len(versions) != len(set(versions)) or versions != sorted(versions):
            raise ValueError("计划快照必须按唯一版本顺序提供")
        return self


class LeaderIntentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: ProjectMessageCategory
    priority: LeaderPriority
    summary: LeaderText


class LeaderOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: LeaderIntentDecision
    action: Literal["dispatch", "continue", "wait_user", "finish", "cancel"]
    summary: LeaderText
    plan: PlanCreate | None = None
    dispatched_task_keys: list[str] = Field(default_factory=list, max_length=100)
    question: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_action_payload(self) -> Self:
        if self.action == "dispatch" and (self.plan is None or not self.dispatched_task_keys):
            raise ValueError("开始新计划必须提供计划和已分派任务")
        if self.action == "continue" and not self.dispatched_task_keys:
            raise ValueError("继续执行必须提供已分派任务")
        if self.action == "wait_user" and not (self.question and self.question.strip()):
            raise ValueError("等待用户时必须提供一个必要问题")
        if self.action != "wait_user" and self.question is not None:
            raise ValueError("只有等待用户时可以携带问题")
        return self


class NextActionKind(StrEnum):
    """Platform-facing scheduling decision after Leader (or deterministic rules)."""

    REPLY = "reply"
    ASK_USER = "ask_user"
    DISPATCH_PRODUCT_MANAGER = "dispatch_product_manager"
    DISPATCH_ARCHITECT = "dispatch_architect"
    DISPATCH_CODE_ENGINEER = "dispatch_code_engineer"
    RETRY_INFRASTRUCTURE = "retry_infrastructure"
    AWAIT_USER_CHALLENGE = "await_user_challenge"
    CANCEL_ACTIVE_RUN = "cancel_active_run"
    NEEDS_LEADER = "needs_leader"


class NextAction(BaseModel):
    """Structured schedule result: Leader proposes semantics; the workflow engine validates."""

    model_config = ConfigDict(extra="forbid")

    action: NextActionKind
    reason_code: str = Field(min_length=1, max_length=80)
    summary: LeaderText
    recipient: TaskRecipient | None = None
    source_artifact_ids: list[str] = Field(default_factory=list, max_length=8)
    required_inputs: list[str] = Field(default_factory=list, max_length=16)
    risk_flags: list[str] = Field(default_factory=list, max_length=16)
    approval_required: bool = False
    reply_text: str | None = Field(default=None, max_length=4000)
    question: str | None = Field(default=None, max_length=2000)
    decided_by: Literal["engine", "leader"] = "engine"
    # Optional Leader-authored plan for message/quality turns that create work.
    plan: PlanCreate | None = None
    dispatched_task_key: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_next_action_payload(self) -> Self:
        if self.action == NextActionKind.REPLY and not (
            self.reply_text and self.reply_text.strip()
        ):
            raise ValueError("reply 动作必须提供 reply_text")
        if self.action == NextActionKind.ASK_USER and not (self.question and self.question.strip()):
            raise ValueError("ask_user 动作必须提供 question")
        dispatch_recipients = {
            NextActionKind.DISPATCH_PRODUCT_MANAGER: TaskRecipient.PRODUCT_MANAGER,
            NextActionKind.DISPATCH_ARCHITECT: TaskRecipient.ARCHITECT,
            NextActionKind.DISPATCH_CODE_ENGINEER: TaskRecipient.CODE_ENGINEER,
        }
        expected_recipient = dispatch_recipients.get(self.action)
        if expected_recipient is not None and self.recipient != expected_recipient:
            raise ValueError("分派动作与 recipient 不一致")
        if expected_recipient is None and self.recipient is not None:
            raise ValueError("非分派动作不得携带 recipient")
        if (
            self.action
            in {
                NextActionKind.DISPATCH_ARCHITECT,
                NextActionKind.DISPATCH_CODE_ENGINEER,
            }
            and not self.source_artifact_ids
        ):
            raise ValueError("工程分派必须引用准确 source_artifact_ids")

        if (self.plan is None) != (self.dispatched_task_key is None):
            raise ValueError("plan 与 dispatched_task_key 必须同时提供")
        if self.plan is not None:
            if expected_recipient is None:
                raise ValueError("只有分派动作可以携带计划")
            if len(self.plan.tasks) != 1:
                raise ValueError("NextAction 每轮只能携带一个可立即分派的任务")
            task = self.plan.tasks[0]
            if task.task_key != self.dispatched_task_key:
                raise ValueError("dispatched_task_key 不在本轮计划中")
            if task.recipient != expected_recipient:
                raise ValueError("计划任务接收角色与分派动作不一致")
            if task.input_configuration_item_ids != self.source_artifact_ids:
                raise ValueError("计划任务输入与 source_artifact_ids 不一致")

        if self.action != NextActionKind.REPLY and self.reply_text is not None:
            raise ValueError("只有 reply 动作可以携带 reply_text")
        if self.action != NextActionKind.ASK_USER and self.question is not None:
            raise ValueError("只有 ask_user 动作可以携带 question")
        return self
