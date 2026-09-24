from __future__ import annotations

import json
from typing import Any, TypedDict

from pydantic import ValidationError

from app.agents.prompts.leader import (
    LEADER_SYSTEM_PROMPT,
    MESSAGE_CLASSIFICATION_SYSTEM_PROMPT,
)
from app.agents.roles import LEADER_PROFILE
from app.agents.tool_protocol import run_bounded_tool_loop
from app.core.exceptions import BusinessException
from app.core.llm import chat_with_tools
from app.schemas.leader import LeaderContext, LeaderOutcome
from app.schemas.project_message_classification import ProjectMessageClassificationDecision
from app.tools.leader import (
    LEADER_TOOLS,
    MESSAGE_CLASSIFICATION_TOOL,
    LeaderToolState,
    execute_leader_tool,
    execute_message_classification_tool,
)

MAX_LEADER_TOOL_TURNS = 12
ALLOWED_LEADER_TOOLS = [tool for tool in LEADER_TOOLS if tool.name in LEADER_PROFILE.allowed_tools]


class ProjectMessageContext(TypedDict):
    """单条历史消息的精简上下文，只给模型看排序与正文。"""

    sequence: int
    sender: str
    content: str


def classify_message(
    *,
    project_name: str,
    project_status: str,
    recent_messages: list[ProjectMessageContext],
    message_sequence: int,
    message_content: str,
) -> ProjectMessageClassificationDecision:
    """调用模型理解消息语义；不执行分类后的任何业务动作。"""

    # 结构化输入：项目状态 + 历史 + 待分类消息，与 prompt 约定字段对齐。
    classification_input = {
        "project": {"name": project_name, "status": project_status},
        "recent_messages_before_target": recent_messages,
        "message_to_classify": {
            "sequence": message_sequence,
            "sender": "user",
            "content": message_content,
        },
    }
    return run_bounded_tool_loop(
        messages=[
            {"role": "system", "content": MESSAGE_CLASSIFICATION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(classification_input, ensure_ascii=False),
            },
        ],
        tools=[MESSAGE_CLASSIFICATION_TOOL],
        allowed_tools={MESSAGE_CLASSIFICATION_TOOL.name},
        role_name="Leader",
        max_turns=1,
        temperature=0.0,
        max_tokens=256,
        missing_message="Leader 未调用工具提交消息分类",
        exhausted_message="Leader 未能提交消息分类",
        execute_tool=execute_message_classification_tool,
        model_call=chat_with_tools,
    )


def _leadership_messages(context: LeaderContext, instruction: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": f"{LEADER_SYSTEM_PROMPT}\n角色目标：{LEADER_PROFILE.goal}",
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "project_id": context.project_id,
                    "run_id": context.run_id,
                    "target_message_id": context.target_message.id,
                    "instruction": instruction,
                },
                ensure_ascii=False,
            ),
        },
    ]


def lead_project_turn(
    context: LeaderContext,
    *,
    instruction: str = "先读取冻结上下文，再管理本轮计划和分派。",
) -> LeaderOutcome:
    """Run one bounded management turn; callers persist validated plan/outcome separately."""

    try:
        context = LeaderContext.model_validate(context.model_dump())
    except ValidationError as exc:
        raise BusinessException("Leader 项目上下文不符合要求") from exc
    state = LeaderToolState(context=context)
    return run_bounded_tool_loop(
        messages=_leadership_messages(context, instruction),
        tools=ALLOWED_LEADER_TOOLS,
        allowed_tools=LEADER_PROFILE.allowed_tools,
        role_name="Leader",
        max_turns=MAX_LEADER_TOOL_TURNS,
        temperature=0.0,
        max_tokens=8192,
        missing_message="Leader 未调用工具更新计划或结束本轮",
        exhausted_message="Leader 工具调用预算已用尽，尚未结束本轮管理",
        execute_tool=lambda call: execute_leader_tool(call, state),
        model_call=chat_with_tools,
    )
