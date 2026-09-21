"""Stable role goals and tool boundaries for task recipients."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.task import TaskRecipient


@dataclass(frozen=True, slots=True)
class AgentRoleProfile:
    display_name: str
    goal: str
    deliverables: tuple[str, ...]
    allowed_tools: frozenset[str]


ROLE_PROFILES: dict[TaskRecipient, AgentRoleProfile] = {
    TaskRecipient.PRODUCT_MANAGER: AgentRoleProfile(
        display_name=TaskRecipient.PRODUCT_MANAGER.value,
        goal="产出可审批 PRD；在用户要求或产品决策需要证据时完成市场与竞品调研。",
        deliverables=("app_spec",),
        allowed_tools=frozenset(
            {
                "browser_open",
                "enhanced_search",
                "search_product_context",
                "edit_prd",
                "write_prd",
            }
        ),
    ),
    TaskRecipient.ARCHITECT: AgentRoleProfile(
        display_name=TaskRecipient.ARCHITECT.value,
        goal="设计简洁、可用、完整的系统，并把已批准 PRD 转化为可实施、可追溯的系统设计。",
        deliverables=("system_design",),
        allowed_tools=frozenset(
            {
                "read_artifact",
                "editor_read",
                "editor_write",
                "terminal_list",
                "terminal_run",
                "write_system_design",
            }
        ),
    ),
    TaskRecipient.CODE_ENGINEER: AgentRoleProfile(
        display_name=TaskRecipient.CODE_ENGINEER.value,
        goal="按照已批准的架构设计和当前任务实现代码、修复缺陷，并通过实际检查保证代码可运行。",
        deliverables=("code",),
        allowed_tools=frozenset(
            {
                "list_files",
                "read_file",
                "search_code",
                "retrieve_code_context",
                "write_new_code",
                "edit_file_by_replace",
                "record_engineering_memory",
                "run_check",
                "complete_work_item",
                "report_blocked",
            }
        ),
    ),
    TaskRecipient.TEST_ENGINEER: AgentRoleProfile(
        display_name=TaskRecipient.TEST_ENGINEER.value,
        goal="独立验证准确代码结果是否符合 PRD 和系统设计，发现缺陷并给出有证据的质量结论。",
        deliverables=("test_report",),
        allowed_tools=frozenset(
            {
                "read_artifact",
                "list_files",
                "read_file",
                "search_code",
                "run_check",
                "record_defect",
                "write_test_report",
            }
        ),
    ),
}

LEADER_PROFILE = AgentRoleProfile(
    display_name="Leader",
    goal=(
        "接收用户信息，判断意图和优先级，拆解任务并分发给合适角色；"
        "持续跟踪计划和岗位结果，决定继续分派、向用户追问或收尾。"
    ),
    deliverables=("classification", "plan", "task_assignment", "leadership_outcome"),
    allowed_tools=frozenset(
        {
            "read_project_context",
            "classify_intent",
            "read_plan",
            "create_plan",
            "update_plan",
            "dispatch_task",
            "read_task_result",
            "request_user_input",
            "finish_turn",
        }
    ),
)


def get_role_profile(recipient: TaskRecipient) -> AgentRoleProfile:
    return ROLE_PROFILES[recipient]
