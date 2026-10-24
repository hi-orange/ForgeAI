"""Independent Test Engineer loop for one exact code result."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.agents.prompts.test_engineer import TEST_ENGINEER_SYSTEM_PROMPT
from app.agents.qa_context import qa_model_messages
from app.agents.roles import get_role_profile
from app.agents.tool_protocol import run_bounded_tool_loop
from app.core.exceptions import BusinessException
from app.core.llm import chat_with_tools
from app.core.settings import settings
from app.models.task import TaskRecipient
from app.schemas.acceptance_test_plan import AcceptanceTestPlan
from app.schemas.agent_action import ToolCall
from app.schemas.app_spec import AppSpec
from app.schemas.qa_suite import QASuite
from app.schemas.system_design import SystemDesign
from app.schemas.test_report import TestReport
from app.services.qa_verification import (
    BASE_QA_TOOL_TURNS,
    MAX_MANUAL_QA_EXECUTION_GRANTS,
    MAX_MANUAL_QA_GRANTS,
    QAVerificationState,
    export_test_engineer_progress,
    resolvable_defect_ids,
    restore_test_engineer_progress,
)
from app.tools.test_engineer import TEST_ENGINEER_TOOLS, execute_test_engineer_tool

MAX_TEST_ENGINEER_TOOL_TURNS = BASE_QA_TOOL_TURNS
TEST_ENGINEER_BUDGET_EXHAUSTED = "Test Engineer 工具调用预算已用尽，尚未提交测试报告"
_BATCH_READ_TOOLS = frozenset({"read_artifact", "list_files", "read_file", "search_code"})
_INSPECTION_GUIDANCE_AFTER_TURNS = 12
MAX_QA_READ_ONLY_TURNS_AFTER_EXECUTION = 12
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
    inherited_suite: QASuite | None = None,
    saved_progress: object = None,
    manual_budget_extension_attempt: int | None = None,
    manual_execution_extension_attempt: int | None = None,
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
    state = QAVerificationState(
        app_spec=spec,
        system_design=system_design,
        code_item_id=code_item_id,
        code_source_hash=code_source_hash,
        workspace_root=root,
        acceptance_test_plan_item_id=acceptance_test_plan_item_id,
        acceptance_test_hash=acceptance_test_hash,
        acceptance_test_plan=acceptance_test_plan,
        thinking_enabled=settings.test_engineer_thinking,
    )
    if inherited_suite is not None:
        from app.services.qa_assets import validate_suite

        validate_suite(inherited_suite, spec, acceptance_test_plan)
        state.suite_versions = [inherited_suite]
    restore_test_engineer_progress(state, saved_progress)
    if manual_budget_extension_attempt is not None:
        if (
            manual_budget_extension_attempt < 2
            or len(state.budget_extension_attempts) >= MAX_MANUAL_QA_GRANTS
            or state.tool_turns != state.tool_budget
            or (
                state.budget_extension_attempt is not None
                and manual_budget_extension_attempt <= state.budget_extension_attempt
            )
        ):
            raise BusinessException("当前 QA 检查点不允许再次扩额")
    if manual_execution_extension_attempt is not None:
        if (
            manual_execution_extension_attempt < 2
            or len(state.execution_extension_attempts) >= MAX_MANUAL_QA_EXECUTION_GRANTS
            or len(state.executions) != state.execution_budget
            or not state.suite_versions
            or (
                state.execution_extension_attempts
                and manual_execution_extension_attempt <= state.execution_extension_attempts[-1]
            )
        ):
            raise BusinessException("当前 QA 检查点不允许再次追加独立执行次数")
    if manual_budget_extension_attempt is not None:
        state.budget_extension_attempts.append(manual_budget_extension_attempt)
    if manual_execution_extension_attempt is not None:
        state.execution_extension_attempts.append(manual_execution_extension_attempt)
    if (
        manual_budget_extension_attempt is not None
        or manual_execution_extension_attempt is not None
    ):
        if on_progress is not None:
            on_progress(export_test_engineer_progress(state))
    if not state.suite_versions:
        preferred_next_tools = [
            *(
                ["design_test_cases"]
                if state.tool_turns >= _INSPECTION_GUIDANCE_AFTER_TURNS
                else ["read_artifact(app_spec)", "design_test_cases"]
            ),
            "write_test_script",
            "run_acceptance_tests",
        ]
    elif state.executions and any(
        row.get("failure_kind") == "script"
        for row in state.executions[-1].get("results", [])
        if isinstance(row, dict)
    ):
        preferred_next_tools = [
            "adapt_test_script (use failed_step_index; inspect a specific file only if needed)",
            "run_acceptance_tests (after suite_hash changes)",
            "record_defect (only for confirmed product failures after script repair)",
        ]
    else:
        preferred_next_tools = [
            "write_test_script",
            "adapt_test_script",
            "run_acceptance_tests",
            'run_check(check_id="all")',
            "capture_screenshots",
            "record_defect",
            "write_test_report",
        ]
    resolvable = resolvable_defect_ids(state)
    if resolvable:
        preferred_next_tools.insert(
            -1,
            "resolve_defect (latest independent execution passed linked tests: "
            + ", ".join(resolvable)
            + ")",
        )
    fixed_context: dict[str, Any] = {
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
            "available_runtime_tool": "run_acceptance_tests",
            "scripts_are_bounded_data": True,
            "api_steps_before_browser_steps": True,
            "smoke_and_screenshots_are_not_case_evidence": True,
        },
        "thinking_enabled": state.thinking_enabled,
        "preferred_next_tools": preferred_next_tools,
    }
    system_message = {
        "role": "system",
        "content": f"{TEST_ENGINEER_SYSTEM_PROMPT}\n{TEST_ENGINEER_PROFILE.contract}",
    }
    messages = qa_model_messages(
        system_message=system_message,
        fixed_context=fixed_context,
        state=state,
        history=[],
    )
    if state.tool_turns >= _INSPECTION_GUIDANCE_AFTER_TURNS and not state.suite_versions:
        messages.append(
            {
                "role": "user",
                "content": (
                    "已保存的 QA 进度仍没有测试套件。冻结验收计划已在上下文中；"
                    "请先调用 design_test_cases 提交覆盖全部验收项的场景，"
                    "再定向读取实现并编写脚本。无需遍历全部源码。"
                ),
            }
        )

    last_read_guidance_turn = -1

    def execute(call: ToolCall) -> tuple[Any, TestReport | None]:
        nonlocal last_read_guidance_turn
        if heartbeat is not None:
            heartbeat()
        previous_execution_count = len(state.executions)
        previous_checks = dict(state.checks)
        observation, output = execute_test_engineer_tool(call, state)
        if call.name in _BATCH_READ_TOOLS and state.executions:
            read_only_turns = state.tool_turns - state.last_qa_progress_turn
            if read_only_turns >= MAX_QA_READ_ONLY_TURNS_AFTER_EXECUTION:
                if on_progress is not None:
                    on_progress(export_test_engineer_progress(state))
                failed_ids = [
                    row["test_id"]
                    for row in state.executions[-1].get("results", [])
                    if isinstance(row, dict)
                    and row.get("status") != "passed"
                    and isinstance(row.get("test_id"), str)
                ]
                cases = ", ".join(failed_ids[:6]) or "无"
                raise BusinessException(
                    "独立 QA 已有执行证据，连续"
                    f" {read_only_turns} 轮只读仍未推进验证；"
                    f"失败用例：{cases}。已保留测试套件与执行结果。"
                    "请修订可适配脚本、记录确认的缺陷，或对冲突的测试前置条件提出标准争议；"
                    "不要继续重复读取源码。"
                )
            if (
                read_only_turns >= MAX_QA_READ_ONLY_TURNS_AFTER_EXECUTION // 2
                and last_read_guidance_turn != state.tool_turns
            ):
                last_read_guidance_turn = state.tool_turns
                observation = observation.model_copy(
                    update={
                        "summary": (
                            f"{observation.summary} | 已有独立执行结果，连续"
                            f" {read_only_turns} 轮只读无进展；请根据失败证据采取 QA 动作。"
                        )
                    }
                )
        elif (
            observation.ok
            or len(state.executions) > previous_execution_count
            or state.checks != previous_checks
        ):
            state.last_qa_progress_turn = state.tool_turns
        if on_progress is not None:
            on_progress(export_test_engineer_progress(state))
        if observation.error_code in _TRANSIENT_EVIDENCE_ERRORS:
            # Infrastructure/source fencing failures are recoverable execution failures,
            # not code defects. Stop immediately instead of spending the remaining budget
            # retrying the same expensive check.
            raise BusinessException(observation.summary)
        remaining = state.tool_budget - state.tool_turns
        if (
            output is None
            and not state.suite_versions
            and state.tool_turns >= _INSPECTION_GUIDANCE_AFTER_TURNS
            and call.name in _BATCH_READ_TOOLS
        ):
            observation = observation.model_copy(
                update={
                    "summary": (
                        f"{observation.summary} | 冻结验收计划已足够先设计场景；"
                        "请调用 design_test_cases，随后再定向补齐脚本所需源码。"
                    )
                }
            )
        if output is None and remaining <= 4:
            if not state.suite_versions:
                next_step = "请先 design_test_cases；没有 suite 不能提交验收报告。"
            elif "all" not in state.checks:
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
        # Reserve before model/tool work: crashes and invalid tool calls do not
        # reset the recovery budget or permit unlimited expensive attempts.
        state.tool_turns += 1
        if on_progress is not None:
            on_progress(export_test_engineer_progress(state))
        kwargs["messages"] = qa_model_messages(
            system_message=system_message,
            fixed_context=fixed_context,
            state=state,
            history=kwargs["messages"],
        )
        return chat_with_tools(
            **kwargs,
            thinking=state.thinking_enabled,
            parallel_tool_calls=True,
        )

    return run_bounded_tool_loop(
        messages=messages,
        tools=ALLOWED_TEST_ENGINEER_TOOLS,
        allowed_tools=TEST_ENGINEER_PROFILE.allowed_tools,
        role_name="Test Engineer",
        max_turns=max(0, state.tool_budget - state.tool_turns),
        temperature=0.0,
        max_tokens=16384 if state.thinking_enabled else 8192,
        missing_message="Test Engineer 未调用工具验证或提交报告",
        exhausted_message=TEST_ENGINEER_BUDGET_EXHAUSTED,
        execute_tool=execute,
        model_call=call_model,
        batch_read_tools=_BATCH_READ_TOOLS,
    )
