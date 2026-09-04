from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models.configuration_item import ConfigurationItemType
from app.models.task import TaskRecipient, TaskStatus

TaskKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$"
    ),
]
ConfigurationItemId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)
]


class TaskCreate(BaseModel):
    """整份计划中的一张任务单；依赖用该计划内的 task_key 表达。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task_key: TaskKey
    recipient: TaskRecipient
    title: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=8000)
    expected_output_type: ConfigurationItemType
    input_configuration_item_ids: list[ConfigurationItemId] = Field(
        default_factory=list, max_length=50
    )
    depends_on_task_keys: list[TaskKey] = Field(default_factory=list, max_length=99)

    @field_validator("input_configuration_item_ids", "depends_on_task_keys")
    @classmethod
    def reject_duplicate_references(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("输入或依赖引用不能重复")
        return values


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: str
    plan_id: str
    task_key: str
    position: int
    recipient: TaskRecipient
    title: str
    instructions: str
    expected_output_type: ConfigurationItemType
    input_configuration_item_ids: list[str]
    depends_on_task_ids: list[str]
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
