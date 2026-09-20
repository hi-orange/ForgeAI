"""Bounded context and outcome contracts for the Manager role."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

from app.models.project_message_classification import ProjectMessageCategory
from app.models.task import TaskRecipient, TaskStatus
from app.schemas.plan import PlanCreate

ManagerText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]


class ManagerPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class ManagerMessageSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    sequence: int = Field(gt=0)
    sender: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1, max_length=8000)


class ManagerTaskSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=40)
    task_key: str = Field(min_length=1, max_length=64)
    recipient: TaskRecipient
    title: str = Field(min_length=1, max_length=200)
    status: TaskStatus
    depends_on_task_ids: list[str] = Field(default_factory=list, max_length=99)
    result: dict[str, JsonValue] | None = None


class ManagerPlanSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=40)
    version: int = Field(ge=1)
    status: Literal["pending", "running", "succeeded", "failed", "cancelled"]
    tasks: list[ManagerTaskSnapshot] = Field(default_factory=list, max_length=100)


class ManagerContext(BaseModel):
    """Frozen data for one management turn; callers select the exact message and run."""

    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(gt=0)
    project_name: str = Field(min_length=1, max_length=200)
    project_status: str = Field(min_length=1, max_length=32)
    run_id: str = Field(min_length=1, max_length=40)
    run_status: str = Field(min_length=1, max_length=32)
    target_message: ManagerMessageSnapshot
    recent_messages: list[ManagerMessageSnapshot] = Field(default_factory=list, max_length=20)
    plans: list[ManagerPlanSnapshot] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_frozen_context(self) -> Self:
        if self.target_message.sender != "user":
            raise ValueError("Manager 只能处理用户目标消息")
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


class ManagerIntentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: ProjectMessageCategory
    priority: ManagerPriority
    summary: ManagerText


class ManagerOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: ManagerIntentDecision
    action: Literal["dispatch", "continue", "wait_user", "finish", "cancel"]
    summary: ManagerText
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
