from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

APP_SPEC_SCHEMA_VERSION: Final = 1

RequirementText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]
RequirementList = Annotated[list[RequirementText], Field(max_length=50)]


class AppSpec(BaseModel):
    """产品意图的正文；约束文档结构，不规定应用必须有哪些页面或数据表。

    所有栏目都必须返回；用户未说明的内容可以为空列表，并通过 open_questions
    标明待确认事项。结构校验不等于已经证明模型没有遗漏或误解需求。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    goal: RequirementText = Field(description="应用要解决的问题和目标")
    target_users: RequirementList = Field(description="用户明确提到的使用人群")
    features: RequirementList = Field(description="用户可见的功能和行为，不是代码实现步骤")
    data_requirements: RequirementList = Field(description="需要记录、展示或处理的业务数据")
    interface_requirements: RequirementList = Field(description="用户提出的界面和交互要求")
    constraints: RequirementList = Field(description="权限、业务规则及其他明确约束")
    acceptance_criteria: RequirementList = Field(description="可观察、可检查且有需求依据的验收条件")
    open_questions: RequirementList = Field(description="信息缺失、冲突或歧义带来的待确认问题")

    @model_validator(mode="after")
    def require_actionable_content_or_questions(self) -> Self:
        if not self.features and not self.open_questions:
            raise ValueError("尚无明确功能时，必须列出待确认问题")
        if not self.acceptance_criteria and not self.open_questions:
            raise ValueError("尚无验收条件时，必须列出待确认问题")
        return self
