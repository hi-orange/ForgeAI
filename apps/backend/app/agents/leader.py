from __future__ import annotations

import json
from typing import Any, TypedDict

from pydantic import ValidationError

from app.agents.prompts.leader import (
    LEADER_SYSTEM_PROMPT,
    MESSAGE_CLASSIFICATION_SYSTEM_PROMPT,
)
from app.agents.roles import LEADER_PROFILE
from app.agents.tool_protocol import append_tool_exchange, require_single_tool_call
from app.core.exceptions import BusinessException
from app.core.llm import chat_completion, chat_with_tools
from app.schemas.leader import LeaderContext, LeaderOutcome
from app.schemas.project_message_classification import ProjectMessageClassificationDecision
from app.tools.leader import (
    LEADER_TOOLS,
    LeaderToolState,
    execute_leader_tool,
)

MAX_LEADER_TOOL_TURNS = 12
ALLOWED_LEADER_TOOLS = [tool for tool in LEADER_TOOLS if tool.name in LEADER_PROFILE.allowed_tools]


class ProjectMessageContext(TypedDict):
    """单条历史消息的精简上下文，只给模型看排序与正文。"""

    sequence: int
    sender: str
    content: str


def _remove_json_fence(value: str) -> str:
    """去掉模型偶发包上的 ```json ... ``` 围栏，便于解析。"""

    stripped = value.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines:
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_decision(raw: str) -> ProjectMessageClassificationDecision:
    """把模型原文校验成固定决策结构；格式不对则视为业务失败。"""

    try:
        payload = json.loads(_remove_json_fence(raw))
        return ProjectMessageClassificationDecision.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise BusinessException("Leader 消息分类返回格式异常") from exc


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
    # temperature=0 降低分类抖动；json_output 要求模型只回 JSON。
    raw = chat_completion(
        messages=[
            {"role": "system", "content": MESSAGE_CLASSIFICATION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(classification_input, ensure_ascii=False),
            },
        ],
        temperature=0.0,
        max_tokens=256,
        json_output=True,
    )
    return _parse_decision(raw)


def _leadership_messages(context: LeaderContext) -> list[dict[str, Any]]:
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
                    "instruction": "先读取冻结上下文，再管理本轮计划和分派。",
                },
                ensure_ascii=False,
            ),
        },
    ]


def lead_project_turn(context: LeaderContext) -> LeaderOutcome:
    """Run one bounded management turn; callers persist validated plan/outcome separately."""

    try:
        context = LeaderContext.model_validate(context.model_dump())
    except ValidationError as exc:
        raise BusinessException("Leader 项目上下文不符合要求") from exc
    state = LeaderToolState(context=context)
    messages = _leadership_messages(context)
    for _turn in range(MAX_LEADER_TOOL_TURNS):
        result = chat_with_tools(
            messages=list(messages),
            tools=ALLOWED_LEADER_TOOLS,
            temperature=0.0,
            max_tokens=8192,
        )
        call = require_single_tool_call(
            result,
            role_name="Leader",
            allowed_tools=LEADER_PROFILE.allowed_tools,
            missing_message="Leader 未调用工具更新计划或结束本轮",
        )
        observation, outcome = execute_leader_tool(call, state)
        if outcome is not None:
            return outcome
        append_tool_exchange(
            messages,
            result=result,
            call=call,
            observation=observation,
        )
    raise BusinessException("Leader 工具调用预算已用尽，尚未结束本轮管理")
