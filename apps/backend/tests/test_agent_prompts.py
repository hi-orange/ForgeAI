"""Prompt contracts keep imported reference ideas inside ForgeAI boundaries."""

import re
import unittest

from app.agents.code_engineer import CODE_ENGINEER_TOOLS
from app.agents.prompts.architect import (
    ARCHITECT_PROMPT_VERSION,
    ARCHITECT_SYSTEM_PROMPT,
)
from app.agents.prompts.code_engineer import (
    CODE_ENGINEER_SYSTEM_PROMPT,
)
from app.agents.prompts.contracts import (
    APPROVAL_WORKFLOW_CONTRACT,
    CONFLICT_PRIORITY_CONTRACT,
    INTERACTION_CONTRACT,
    LEADER_DISPATCH_CONTRACT,
    PROMPT_CONTRACT_VERSION,
    QUALITY_GATE_CONTRACT,
    TOOL_EXECUTION_CONTRACT,
    VISUAL_SYSTEM_CONTRACT,
)
from app.agents.prompts.leader import LEADER_SYSTEM_PROMPT, MESSAGE_CLASSIFICATION_SYSTEM_PROMPT
from app.agents.prompts.product_manager import (
    APP_SPEC_PROMPT_VERSION,
    APP_SPEC_SYSTEM_PROMPT,
)


class AgentPromptContractTests(unittest.TestCase):
    def test_engineering_tool_descriptions_accept_current_source_context(self):
        descriptions = {tool.name: tool.description for tool in CODE_ENGINEER_TOOLS}
        for tool_name in ("write_files", "edit_file_by_replace"):
            with self.subTest(tool=tool_name):
                self.assertIn("初始源码上下文", descriptions[tool_name])
        self.assertIn("已有上下文", descriptions["read_file"])

    def test_engineer_prompt_matches_the_active_tool_loop(self):
        prompt_identifiers = set(re.findall(r"[A-Za-z][A-Za-z0-9_]*", CODE_ENGINEER_SYSTEM_PROMPT))
        exposed_tools = {tool.name for tool in CODE_ENGINEER_TOOLS}
        for tool_name in (
            "read_file",
            "write_files",
            "edit_file_by_replace",
            "update_delivery_plan",
            "run_check",
            "complete_work_item",
        ):
            with self.subTest(tool=tool_name):
                self.assertIn(tool_name, exposed_tools)
                self.assertIn(tool_name, prompt_identifiers)
        for retired_tool in ("write_file", "retrieve_code_context", "write_new_code"):
            with self.subTest(retired_tool=retired_tool):
                self.assertNotIn(retired_tool, exposed_tools)
                self.assertNotIn(retired_tool, prompt_identifiers)
        self.assertIn("观察已有状态", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("平台按真实源码变更选择检查", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("基础契约 quick", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("仅样式或图片变化 frontend", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("业务、高风险或未知变更 all", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn(
            "声明 provides_requirement_ids、QA 修复和最后一项必须 all", CODE_ENGINEER_SYSTEM_PROMPT
        )
        self.assertIn("局部检查不能消除独立 QA 缺陷或作为最终交付证据", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("冻结验收资产", CODE_ENGINEER_SYSTEM_PROMPT)

    def test_product_manager_research_is_optional_and_approval_safe(self):
        self.assertTrue(APP_SPEC_PROMPT_VERSION.endswith("_v17"))
        self.assertIn("条目顺序不是工程施工顺序", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("普通、明确的应用需求不强制联网", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("需由用户勾选、编辑和批准", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("source_ids", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("短标题：一句用户可见说明", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("完整能力地图", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("不同权限边界", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("platform_capabilities.image_generation", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("禁止套用固定范文", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("不做市场/竞品调研专章", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("不自动等于用户确认", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("SQLite", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("PostgreSQL", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("刷新后仍能读取持久化结果", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("开发种子数据", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("默认仅要求公开内容", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("不要由初始内容推断可登录演示账号", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("仅供隔离预览或测试", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("utilitarian", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("awaiting_approval", APP_SPEC_SYSTEM_PROMPT)
        self.assertNotIn("初始化全栈项目：Vue", APP_SPEC_SYSTEM_PROMPT)
        self.assertNotIn("视觉设计：…风格要点", APP_SPEC_SYSTEM_PROMPT)
        self.assertNotIn("模式 2", APP_SPEC_SYSTEM_PROMPT)

    def test_architect_produces_implementation_ready_cross_layer_contracts(self):
        self.assertTrue(ARCHITECT_PROMPT_VERSION.endswith("_v10"))
        self.assertIn("implementation_dependencies", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("update_delivery_plan", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("批准功能 → 模块 → 接口 → 数据结构", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("method/path", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("SQLite", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("PostgreSQL", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("forgeai.smoke.json", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("工具型产品不强制图片或插画", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("ui_state", INTERACTION_CONTRACT)
        self.assertIn("不强制图片或插画", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("不为浏览内容自动创建可登录账号", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("凭据经私密配置按项目生成", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("发布时默认不创建或启用演示", ARCHITECT_SYSTEM_PROMPT)

    def test_leader_routes_by_intent_complexity_and_exact_results(self):
        self.assertIn("breaking_public_contract", LEADER_SYSTEM_PROMPT)
        self.assertIn("必须先安排 Architect", LEADER_SYSTEM_PROMPT)
        self.assertIn("不预写尚未冻结输入的完整 DAG", LEADER_SYSTEM_PROMPT)
        self.assertIn("准确 task_id", LEADER_SYSTEM_PROMPT)
        self.assertIn("reason code", LEADER_SYSTEM_PROMPT)
        self.assertIn("request_user_input 只用于", LEADER_SYSTEM_PROMPT)
        self.assertIn("工作流引擎自动交给 Test Engineer", LEADER_SYSTEM_PROMPT)
        self.assertIn("优先选择 implementation_repair", MESSAGE_CLASSIFICATION_SYSTEM_PROMPT)

    def test_shared_prompt_contracts_have_one_versioned_source(self):
        self.assertEqual(PROMPT_CONTRACT_VERSION, "forgeai_prompt_contracts_v5")
        self.assertIn("安全、权限、所有权", CONFLICT_PRIORITY_CONTRACT)
        self.assertIn("awaiting_approval", APPROVAL_WORKFLOW_CONTRACT)
        self.assertIn("批量读取", TOOL_EXECUTION_CONTRACT)
        self.assertIn("utilitarian", VISUAL_SYSTEM_CONTRACT)
        self.assertIn("纯 UI", INTERACTION_CONTRACT)
        self.assertIn("最低联调门禁", QUALITY_GATE_CONTRACT)
        self.assertIn("仅有一个合法后续", LEADER_DISPATCH_CONTRACT)
        self.assertIn("BLOCKED", LEADER_DISPATCH_CONTRACT)
        self.assertIn("不得从自由文本关键词猜测角色", LEADER_DISPATCH_CONTRACT)


if __name__ == "__main__":
    unittest.main()
