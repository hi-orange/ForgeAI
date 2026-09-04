from datetime import datetime
from graphlib import CycleError, TopologicalSorter
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.plan import PlanStatus
from app.schemas.task import TaskCreate


class PlanCreate(BaseModel):
    """保存调用方已经确定的完整计划；不负责 LLM 规划或自动拆分任务。"""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, le=2_147_483_647)
    cause_message_id: int = Field(gt=0)
    tasks: list[TaskCreate] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_task_graph(self) -> Self:
        keys = {task.task_key for task in self.tasks}
        if len(keys) != len(self.tasks):
            raise ValueError("同一计划中的 task_key 不能重复")
        for task in self.tasks:
            if task.task_key in task.depends_on_task_keys:
                raise ValueError("任务不能依赖自己")
            if not set(task.depends_on_task_keys).issubset(keys):
                raise ValueError("依赖任务必须来自本次提交的同一计划")
        graph = {task.task_key: task.depends_on_task_keys for task in self.tasks}
        try:
            tuple(TopologicalSorter(graph).static_order())
        except CycleError as exc:
            raise ValueError("任务依赖不能形成循环") from exc
        return self


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    plan_id: str
    project_id: int
    build_run_id: str
    version: int
    cause_message_id: int
    definition_hash: str
    status: PlanStatus
    created_at: datetime
    updated_at: datetime
