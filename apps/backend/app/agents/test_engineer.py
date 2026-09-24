"""Independent Test Engineer loop for one exact code result."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.agents.prompts.test_engineer import TEST_ENGINEER_SYSTEM_PROMPT
from app.agents.roles import get_role_profile
from app.agents.tool_protocol import run_bounded_tool_loop
from app.core.exceptions import BusinessException
from app.core.llm import chat_with_tools
from app.models.task import TaskRecipient
from app.schemas.agent_action import ToolCall
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.schemas.test_report import TestReport
from app.tools.test_engineer import (
    TEST_ENGINEER_TOOLS,
    TestEngineerToolState,
    execute_test_engineer_tool,
)

MAX_TEST_ENGINEER_TOOL_TURNS = 24
TEST_ENGINEER_PROFILE = get_role_profile(TaskRecipient.TEST_ENGINEER)
ALLOWED_TEST_ENGINEER_TOOLS = [
    tool for tool in TEST_ENGINEER_TOOLS if tool.name in TEST_ENGINEER_PROFILE.allowed_tools
]


def verify_code(
    *,
    spec: AppSpec,
    code_item_id: str,
    code_source_hash: str,
    workspace_root: Path,
    system_design: SystemDesign | None,
) -> TestReport:
    """Verify one frozen code identity; persistence remains the caller's responsibility."""

    try:
        spec = AppSpec.model_validate(spec.model_dump())
        if system_design is not None:
            system_design = SystemDesign.model_validate(system_design.model_dump())
    except ValidationError as exc:
        raise BusinessException("Test Engineer 的 PRD 或系统设计输入不符合要求") from exc
    root = workspace_root.resolve()
    if not root.is_dir():
        raise BusinessException("Test Engineer 代码工作区不存在")
    if len(code_source_hash) != 64 or any(
        character not in "0123456789abcdef" for character in code_source_hash
    ):
        raise BusinessException("Test Engineer 代码 source_hash 格式异常")
    state = TestEngineerToolState(
        app_spec=spec,
        system_design=system_design,
        code_item_id=code_item_id,
        code_source_hash=code_source_hash,
        workspace_root=root,
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": f"{TEST_ENGINEER_SYSTEM_PROMPT}\n角色目标：{TEST_ENGINEER_PROFILE.goal}",
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "code_item_id": code_item_id,
                    "source_hash": code_source_hash,
                    "acceptance_ids": [item.id for item in spec.acceptance_criteria],
                    "has_system_design": system_design is not None,
                    "tool_budget": MAX_TEST_ENGINEER_TOOL_TURNS,
                    "preferred_next_tools": [
                        "read_artifact(app_spec)",
                        'run_check(check_id="all")',
                        "write_test_report",
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]
    turns_used = 0

    def execute(call: ToolCall) -> tuple[Any, TestReport | None]:
        nonlocal turns_used
        observation, output = execute_test_engineer_tool(call, state)
        turns_used += 1
        remaining = MAX_TEST_ENGINEER_TOOL_TURNS - turns_used
        if output is None and remaining <= 4:
            nudge = f"还剩 {remaining} 轮工具预算。" + (
                '请先 run_check(check_id="all")，再 write_test_report。'
                if "all" not in state.checks
                else "请尽快 write_test_report 结束本轮验证。"
            )
            observation = observation.model_copy(
                update={"summary": f"{observation.summary} | {nudge}"}
            )
        return observation, output

    return run_bounded_tool_loop(
        messages=messages,
        tools=ALLOWED_TEST_ENGINEER_TOOLS,
        allowed_tools=TEST_ENGINEER_PROFILE.allowed_tools,
        role_name="Test Engineer",
        max_turns=MAX_TEST_ENGINEER_TOOL_TURNS,
        temperature=0.0,
        max_tokens=8192,
        missing_message="Test Engineer 未调用工具验证或提交报告",
        exhausted_message="Test Engineer 工具调用预算已用尽，尚未提交测试报告",
        execute_tool=execute,
        model_call=chat_with_tools,
    )
