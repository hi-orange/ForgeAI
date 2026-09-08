from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.app_spec import APP_SPEC_SCHEMA_VERSION, AppSpec

MAX_REQUIREMENT_CHARS = 8000
MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARS = 8000


class RequirementMessage(BaseModel):
    """模型实际看到的一条完整消息，保留原编号与顺序。"""

    model_config = ConfigDict(extra="forbid", strict=True, from_attributes=True)

    id: int = Field(gt=0)
    sequence: int = Field(gt=0)
    sender: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(min_length=1, max_length=MAX_REQUIREMENT_CHARS)]


class ProductManagerInput(BaseModel):
    """一次模型调用的输入副本，不包含可变的项目名称、简介或最新消息。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    task_instructions: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)
    ]
    source_message: RequirementMessage
    recent_messages: list[RequirementMessage] = Field(max_length=MAX_HISTORY_MESSAGES)
    context_truncated: bool
    previous_app_spec: AppSpec | None = None
    previous_item_id: str | None = Field(default=None, min_length=1, max_length=40)

    @model_validator(mode="after")
    def validate_message_window(self) -> Self:
        if (self.previous_app_spec is None) != (self.previous_item_id is None):
            raise ValueError("原需求正文和成果编号必须同时提供")
        if self.source_message.sender != "user":
            raise ValueError("触发需求必须来自用户")
        ids = [message.id for message in self.recent_messages] + [self.source_message.id]
        sequences = [message.sequence for message in self.recent_messages] + [
            self.source_message.sequence
        ]
        if len(ids) != len(set(ids)) or any(
            previous >= current for previous, current in pairwise(sequences)
        ):
            raise ValueError("历史消息必须唯一、按序排列，且早于触发消息")
        if sum(len(message.content) for message in self.recent_messages) > MAX_HISTORY_CHARS:
            raise ValueError("历史消息超过上下文长度限制")
        if any(
            not message.content.strip() for message in [*self.recent_messages, self.source_message]
        ):
            raise ValueError("需求消息不能为空白")
        return self


class ProductManagerResult(BaseModel):
    """尚未登记的需求整理结果；来源信息由服务填写，不由模型生成。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    project_id: int = Field(gt=0)
    build_run_id: str = Field(min_length=1, max_length=40)
    plan_id: str = Field(min_length=1, max_length=40)
    task_id: str = Field(min_length=1, max_length=40)
    cause_message_id: int = Field(gt=0)
    source_message_ids: list[Annotated[int, Field(gt=0)]] = Field(
        min_length=1, max_length=MAX_HISTORY_MESSAGES + 1
    )
    context_truncated: bool
    schema_version: Literal[1] = APP_SPEC_SCHEMA_VERSION
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    prompt_version: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
    ]
    app_spec: AppSpec
    input_configuration_item_ids: list[str] = Field(default_factory=list, max_length=1)
    execution_id: str | None = Field(default=None, min_length=1, max_length=40)

    @model_validator(mode="after")
    def validate_source_ids(self) -> Self:
        if len(self.source_message_ids) != len(set(self.source_message_ids)):
            raise ValueError("来源消息编号不能重复")
        if self.source_message_ids[-1] != self.cause_message_id:
            raise ValueError("来源消息必须以触发需求的消息结束")
        return self
