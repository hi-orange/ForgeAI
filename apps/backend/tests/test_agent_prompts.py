"""Prompt contracts keep imported reference ideas inside ForgeAI boundaries."""

import unittest

from app.agents.prompts.architect import (
    ARCHITECT_PROMPT_VERSION,
    ARCHITECT_SYSTEM_PROMPT,
)
from app.agents.prompts.code_engineer import (
    CODE_ENGINEER_SYSTEM_PROMPT,
    CODE_WRITER_SYSTEM_PROMPT,
    FILE_PLANNER_SYSTEM_PROMPT,
)
from app.agents.prompts.contracts import (
    APPROVAL_WORKFLOW_CONTRACT,
    CONFLICT_PRIORITY_CONTRACT,
    INTERACTION_CONTRACT,
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
    def test_product_manager_research_is_optional_and_approval_safe(self):
        self.assertTrue(APP_SPEC_PROMPT_VERSION.endswith("_v14"))
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
        self.assertIn("React + TypeScript + FastAPI + SQLite", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("刷新后仍能读取持久化结果", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("开发种子数据", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("utilitarian", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("awaiting_approval", APP_SPEC_SYSTEM_PROMPT)
        self.assertNotIn("初始化全栈项目：Vue", APP_SPEC_SYSTEM_PROMPT)
        self.assertNotIn("视觉设计：…风格要点", APP_SPEC_SYSTEM_PROMPT)
        self.assertNotIn("模式 2", APP_SPEC_SYSTEM_PROMPT)

    def test_architect_produces_implementation_ready_cross_layer_contracts(self):
        self.assertTrue(ARCHITECT_PROMPT_VERSION.endswith("_v5"))
        self.assertIn("批准功能 → 模块 → 接口 → 数据结构", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("method/path", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("React、TypeScript、FastAPI 和 SQLite", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("forgeai.smoke.json", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("工具型产品不强制图片或插画", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("ui_state", INTERACTION_CONTRACT)
        self.assertIn("不强制图片或插画", ARCHITECT_SYSTEM_PROMPT)

    def test_code_prompts_require_observation_and_bounded_single_file_writes(self):
        self.assertIn("观察已有状态", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("retrieve_code_context", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("RAG 命中不是源码", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("edit_file_by_replace", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("install_project_dependency", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("禁止 apt", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("一次工具调用只修改一个文件", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("真实业务 API", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("默认蓝白卡片", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("不得借新增功能擅自换色", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("engineering_context.smoke_contract", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("不得虚构 seed_checks", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("frontend/src/index.css", FILE_PLANNER_SYSTEM_PROMPT)
        self.assertIn("kind=image", FILE_PLANNER_SYSTEM_PROMPT)
        self.assertIn("whole-file 编辑格式", CODE_WRITER_SYSTEM_PROMPT)
        self.assertIn("不输出 TODO", CODE_WRITER_SYSTEM_PROMPT)
        self.assertIn("不可互相替代", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("平台装配多个已知路径", CODE_ENGINEER_SYSTEM_PROMPT)

    def test_leader_routes_by_intent_complexity_and_exact_results(self):
        self.assertIn("breaking_public_contract", LEADER_SYSTEM_PROMPT)
        self.assertIn("必须先安排 Architect", LEADER_SYSTEM_PROMPT)
        self.assertIn("完整 DAG", LEADER_SYSTEM_PROMPT)
        self.assertIn("准确 task_id", LEADER_SYSTEM_PROMPT)
        self.assertIn("reason code", LEADER_SYSTEM_PROMPT)
        self.assertIn("request_user_input 只用于", LEADER_SYSTEM_PROMPT)
        self.assertIn("优先选择 implementation_repair", MESSAGE_CLASSIFICATION_SYSTEM_PROMPT)

    def test_shared_prompt_contracts_have_one_versioned_source(self):
        self.assertEqual(PROMPT_CONTRACT_VERSION, "forgeai_prompt_contracts_v1")
        self.assertIn("安全、权限、所有权", CONFLICT_PRIORITY_CONTRACT)
        self.assertIn("awaiting_approval", APPROVAL_WORKFLOW_CONTRACT)
        self.assertIn("批量读取", TOOL_EXECUTION_CONTRACT)
        self.assertIn("utilitarian", VISUAL_SYSTEM_CONTRACT)
        self.assertIn("纯 UI", INTERACTION_CONTRACT)
        self.assertIn("最低联调门禁", QUALITY_GATE_CONTRACT)


if __name__ == "__main__":
    unittest.main()
