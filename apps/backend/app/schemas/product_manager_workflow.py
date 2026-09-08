from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

WorkflowId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=40),
]


class ProductManagerWorkflowOutcome(StrEnum):
    """产品需求阶段结束后，调用方下一步应采取的动作。"""

    NEEDS_USER_INPUT = "needs_user_input"
    READY_FOR_DESIGN = "ready_for_design"


class ProductManagerWorkflowInput(BaseModel):
    """启动本轮需求工作流所需的已保存业务标识。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: int = Field(gt=0)
    recovery_execution_id: WorkflowId | None = None
    project_id: int = Field(gt=0)
    build_run_id: WorkflowId
    cause_message_id: int = Field(gt=0)


class ProductManagerWorkflowResult(BaseModel):
    """产品需求阶段的可验证结果；不携带未发布的模型草稿。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    project_id: int = Field(gt=0)
    build_run_id: WorkflowId
    cause_message_id: int = Field(gt=0)
    plan_id: WorkflowId
    task_id: WorkflowId
    configuration_item_id: WorkflowId
    outcome: ProductManagerWorkflowOutcome = Field(strict=False)
    open_questions: list[str] = Field(max_length=50)
    design_plan_id: WorkflowId | None = None
    design_task_id: WorkflowId | None = None

    @model_validator(mode="after")
    def validate_design_assignment(self) -> Self:
        if (self.design_plan_id is None) != (self.design_task_id is None):
            raise ValueError("设计计划和任务编号必须同时提供")
        if self.design_task_id is not None and (
            self.outcome != ProductManagerWorkflowOutcome.READY_FOR_DESIGN or self.open_questions
        ):
            raise ValueError("有待确认问题的需求不能携带设计派工结果")
        return self
