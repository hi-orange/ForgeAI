from __future__ import annotations

import json
from typing import TypedDict

from pydantic import ValidationError

from app.agents.prompts.project_manager import MESSAGE_CLASSIFICATION_SYSTEM_PROMPT
from app.core.exceptions import BusinessException
from app.core.llm import chat_completion
from app.schemas.project_message_classification import ProjectMessageClassificationDecision


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
        raise BusinessException("ProjectManager 消息分类返回格式异常") from exc


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
