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
    responsibility: str = ""
    handoff: str = ""

    @property
    def contract(self) -> str:
        return f"角色目标：{self.goal}\n职责边界：{self.responsibility}\n交接条件：{self.handoff}"


ROLE_PROFILES: dict[TaskRecipient, AgentRoleProfile] = {
    TaskRecipient.PRODUCT_MANAGER: AgentRoleProfile(
        display_name=TaskRecipient.PRODUCT_MANAGER.value,
        goal=("定义用户问题、批准范围、非目标与可观察成功条件，而不是替工程师编写实现任务。"),
        responsibility=(
            "拥有 app_spec；区分用户事实、假设和待确认问题；保留未变更需求"
            "身份。不得决定模块、文件或技术实现，不得批准自己的需求。"
        ),
        handoff=(
            "提交可逐项批准的用户能力及有来源的验收标准；有歧义显式列 open_q"
            "uestions；只有用户批准后才能交付。"
        ),
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
        responsibility=(
            "拥有 system_design；解决模块依赖、接口、数据所有权、迁移"
            "和技术风险；给出取舍理由，不规定工程师的逐文件动作。不得扩展产品范围或"
            "修改源码。"
        ),
        handoff=(
            "覆盖获批能力的跨层契约；接口输入输出、失败行为与持久化职责一致；设计不"
            "能替代源码观察或运行验证。"
        ),
        deliverables=("system_design",),
        allowed_tools=frozenset(
            {
                "read_artifact",
                "editor_read",
                "editor_write",
                "editor_patch",
                "terminal_list",
                "terminal_run",
                "write_system_design",
            }
        ),
    ),
    TaskRecipient.CODE_ENGINEER: AgentRoleProfile(
        display_name=TaskRecipient.CODE_ENGINEER.value,
        goal="根据冻结意图和设计，以真实源码与工具反馈驱动实现、局部修改和有证据的缺陷修复。",
        responsibility=(
            "拥有生成源码与开发期 smoke；自主选择读取、修改、验证动作；平台拥"
            "有权限、预算、恢复和完成门禁。不得改批准范围、冻结验收资产或自行宣布验"
            "收通过。"
        ),
        handoff=(
            "提供通过工程检查的准确 source_hash；把候选代码交给独立测试"
            "师，不能把模型自述完成当作成功。"
        ),
        deliverables=("code",),
        allowed_tools=frozenset(
            {
                "update_delivery_plan",
                "list_files",
                "read_file",
                "search_code",
                "write_files",
                "generate_api_client",
                "install_capability",
                "edit_file_by_replace",
                "generate_image",
                "install_project_dependency",
                "run_check",
                "complete_work_item",
                "report_blocked",
            }
        ),
    ),
    TaskRecipient.TEST_ENGINEER: AgentRoleProfile(
        display_name=TaskRecipient.TEST_ENGINEER.value,
        goal="独立验证准确代码结果是否符合 PRD 和系统设计，发现缺陷并给出有证据的质量结论。",
        responsibility=(
            "拥有 test_report；按冻结验收逐项核验证据，区分实现缺陷、环"
            "境故障和合同冲突。源码只读，不代替工程师修复，不弱化测试。"
        ),
        handoff=(
            "报告绑定准确 source_hash 与 test_hash；每项结论"
            "有执行证据，缺证据为 blocked，合同冲突进入 challenge"
            "；发布仍由平台门禁决定。"
        ),
        deliverables=("test_report",),
        allowed_tools=frozenset(
            {
                "design_test_cases",
                "write_test_script",
                "run_acceptance_tests",
                "read_artifact",
                "list_files",
                "read_file",
                "search_code",
                "run_check",
                "capture_screenshots",
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
