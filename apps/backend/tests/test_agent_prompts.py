"""Prompt contracts keep imported reference ideas inside ForgeAI boundaries."""

import unittest

from app.agents.prompts.architect import (
    ARCHITECT_PROMPT_VERSION,
    ARCHITECT_SYSTEM_PROMPT,
)
from app.agents.prompts.code_engineer import (
    CODE_ENGINEER_SYSTEM_PROMPT,
    CODE_WRITER_SYSTEM_PROMPT,
)
from app.agents.prompts.leader import LEADER_SYSTEM_PROMPT
from app.agents.prompts.product_manager import (
    APP_SPEC_PROMPT_VERSION,
    APP_SPEC_SYSTEM_PROMPT,
)


class AgentPromptContractTests(unittest.TestCase):
    def test_product_manager_research_is_optional_and_approval_safe(self):
        self.assertTrue(APP_SPEC_PROMPT_VERSION.endswith("_v3"))
        self.assertIn("普通、明确的应用需求不强制联网", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("需由用户勾选、编辑和批准", APP_SPEC_SYSTEM_PROMPT)
        self.assertIn("source_ids", APP_SPEC_SYSTEM_PROMPT)

    def test_architect_produces_implementation_ready_cross_layer_contracts(self):
        self.assertTrue(ARCHITECT_PROMPT_VERSION.endswith("_v2"))
        self.assertIn("批准功能 → 模块 → 接口 → 数据结构", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("method/path", ARCHITECT_SYSTEM_PROMPT)
        self.assertIn("FastAPI、Vue 和 SQLite", ARCHITECT_SYSTEM_PROMPT)

    def test_code_prompts_require_observation_and_bounded_single_file_writes(self):
        self.assertIn("观察已有状态", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("retrieve_code_context", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("RAG 命中不是源码", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("edit_file_by_replace", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("install_project_dependency", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("禁止 apt", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("一次工具调用只修改一个文件", CODE_ENGINEER_SYSTEM_PROMPT)
        self.assertIn("只输出目标文件正文", CODE_WRITER_SYSTEM_PROMPT)
        self.assertIn("不输出 TODO", CODE_WRITER_SYSTEM_PROMPT)

    def test_leader_routes_by_intent_complexity_and_exact_results(self):
        self.assertIn("简单", LEADER_SYSTEM_PROMPT)
        self.assertIn("先安排 Architect", LEADER_SYSTEM_PROMPT)
        self.assertIn("完整 DAG", LEADER_SYSTEM_PROMPT)
        self.assertIn("准确 task_id", LEADER_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
