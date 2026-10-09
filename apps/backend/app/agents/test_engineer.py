"""Independent Test Engineer loop for one exact code result."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.agents.prompts.test_engineer import TEST_ENGINEER_SYSTEM_PROMPT
from app.agents.roles import get_role_profile
from app.agents.tool_protocol import run_bounded_tool_loop
from app.core.exceptions import BusinessException
from app.core.llm import chat_with_tools
from app.models.task import TaskRecipient
from app.schemas.acceptance_test_plan import AcceptanceTestPlan
from app.schemas.agent_action import ToolCall
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.schemas.test_report import TestReport
from app.tools.test_engineer import (
    TEST_ENGINEER_TOOLS,
    TestEngineerToolState,
    execute_test_engineer_tool,
    export_test_engineer_progress,
    restore_test_engineer_progress,
)

MAX_TEST_ENGINEER_TOOL_TURNS = 24
_TRANSIENT_EVIDENCE_ERRORS = frozenset(
    {
        "CHECK_ENVIRONMENT_UNAVAILABLE",
        "CHECK_TIMEOUT",
        "SOURCE_CHANGED",
        "SOURCE_MISMATCH",
    }
)
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
    acceptance_test_plan_item_id: str | None = None,
    acceptance_test_hash: str | None = None,
    acceptance_test_plan: AcceptanceTestPlan | None = None,
    saved_progress: object = None,
    heartbeat: Callable[[], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
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
        acceptance_test_plan_item_id=acceptance_test_plan_item_id,
        acceptance_test_hash=acceptance_test_hash,
        acceptance_test_plan=acceptance_test_plan,
    )
    restore_test_engineer_progress(state, saved_progress)
    resumed_progress = export_test_engineer_progress(state)
    if "all" not in state.checks:
        preferred_next_tools = [
            "read_artifact(app_spec)",
            "read_artifact(acceptance_test_plan)",
            'run_check(check_id="all")',
            'capture_screenshots(route="/")',
            "write_test_report",
        ]
    elif not any(check_id.startswith("visual") for check_id in state.checks):
        preferred_next_tools = ['capture_screenshots(route="/")', "write_test_report"]
    else:
        preferred_next_tools = ["write_test_report"]
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
                    "test_plan_item_id": acceptance_test_plan_item_id,
                    "test_hash": acceptance_test_hash,
                    "acceptance_test_plan": (
                        acceptance_test_plan.model_dump(mode="json")
                        if acceptance_test_plan is not None
                        else None
                    ),
                    "acceptance_execution_contract": {
                        "available_runtime_tool": 'run_check(check_id="all")',
                        "available_browser_tool": 'capture_screenshots(route="/")',
                        "target_paths_are_logical": True,
                        "missing_logical_target_is_not_a_code_defect": True,
                    },
                    "tool_budget": MAX_TEST_ENGINEER_TOOL_TURNS,
                    "preferred_next_tools": preferred_next_tools,
                    "resumed_progress": resumed_progress,
                },
                ensure_ascii=False,
            ),
        },
    ]
    turns_used = 0

    def execute(call: ToolCall) -> tuple[Any, TestReport | None]:
        nonlocal turns_used
        if heartbeat is not None:
            heartbeat()
        observation, output = execute_test_engineer_tool(call, state)
        turns_used += 1
        if on_progress is not None:
            on_progress(export_test_engineer_progress(state))
        if observation.error_code in _TRANSIENT_EVIDENCE_ERRORS:
            # Infrastructure/source fencing failures are recoverable execution failures,
            # not code defects. Stop immediately instead of spending the remaining budget
            # retrying the same expensive check.
            raise BusinessException(observation.summary)
        remaining = MAX_TEST_ENGINEER_TOOL_TURNS - turns_used
        if output is None and remaining <= 4:
            if "all" not in state.checks:
                next_step = '请先 run_check(check_id="all")。'
            elif not any(check_id.startswith("visual") for check_id in state.checks):
                next_step = '请立刻 capture_screenshots(route="/")。'
            else:
                next_step = "请尽快 write_test_report 结束本轮验证。"
            nudge = f"还剩 {remaining} 轮工具预算。{next_step}"
            observation = observation.model_copy(
                update={"summary": f"{observation.summary} | {nudge}"}
            )
        return observation, output

    def call_model(**kwargs: Any):
        if heartbeat is not None:
            heartbeat()
        all_check = state.checks.get("all")
        visual_checks = [
            result for check_id, result in state.checks.items() if check_id.startswith("visual")
        ]
        if all_check is not None and not visual_checks:
            # Screenshot evidence is mandatory and should happen exactly once after all.
            kwargs["tools"] = [
                tool for tool in ALLOWED_TEST_ENGINEER_TOOLS if tool.name == "capture_screenshots"
            ]
        elif all_check is not None and visual_checks:
            has_code_failure = any(
                not result.ok and result.error_code not in _TRANSIENT_EVIDENCE_ERRORS
                for result in [all_check, *visual_checks]
            )
            final_tool_names = (
                {
                    "read_artifact",
                    "list_files",
                    "read_file",
                    "search_code",
                    "record_defect",
                    "write_test_report",
                }
                if has_code_failure
                else {"write_test_report"}
            )
            kwargs["tools"] = [
                tool for tool in ALLOWED_TEST_ENGINEER_TOOLS if tool.name in final_tool_names
            ]
        return chat_with_tools(**kwargs)

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
        model_call=call_model,
    )
