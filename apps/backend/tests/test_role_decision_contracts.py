"""Role redesign is enforced at submission/tool boundaries, not only in prose."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_architect import valid_design
from test_architect import valid_spec as architect_spec
from test_product_manager import model_input, valid_spec

from app.agents.roles import ROLE_PROFILES
from app.agents.tool_protocol import MAX_TOOL_RESULT_CHARS, tool_observation_json
from app.models.task import TaskRecipient
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.tools.architect import ArchitectToolState, execute_architect_tool
from app.tools.product_manager import ProductManagerToolState, execute_product_manager_tool


class RoleDecisionContractTests(unittest.TestCase):
    def test_four_roles_have_explicit_responsibility_and_handoff(self):
        self.assertEqual(len(ROLE_PROFILES), 4)
        for recipient, profile in ROLE_PROFILES.items():
            with self.subTest(role=recipient):
                self.assertTrue(profile.responsibility)
                self.assertTrue(profile.handoff)
                if recipient != TaskRecipient.CODE_ENGINEER:
                    self.assertTrue(
                        {"write_file", "edit_file_by_replace"}.isdisjoint(profile.allowed_tools)
                    )
        self.assertNotIn(
            "write_test_report", ROLE_PROFILES[TaskRecipient.CODE_ENGINEER].allowed_tools
        )

    def test_pm_rejects_missing_or_invented_acceptance_sources(self):
        for sources in ([], ["invented_feature"]):
            with self.subTest(sources=sources):
                spec = valid_spec()
                spec["acceptance_criteria"][0]["source_ids"] = sources
                state = ProductManagerToolState(payload=model_input())
                observation, output = execute_product_manager_tool(
                    ToolCall(id="submit", name="write_prd", arguments={"prd": spec}),
                    state,
                )
                self.assertFalse(observation.ok)
                self.assertIsNone(output)
                self.assertIsNone(state.draft)

    def test_pm_cannot_submit_uncovered_scope_as_fully_resolved(self):
        spec = valid_spec(open_questions=[])
        spec["acceptance_criteria"][0]["source_ids"] = ["feat_track"]
        observation, output = execute_product_manager_tool(
            ToolCall(id="submit", name="write_prd", arguments={"prd": spec}),
            ProductManagerToolState(payload=model_input()),
        )
        self.assertFalse(observation.ok)
        self.assertIsNone(output)

    def test_architect_submission_requires_frozen_intent_read(self):
        with TemporaryDirectory(prefix="forgeai-role-contract-") as directory:
            state = ArchitectToolState(app_spec=architect_spec(), workspace_root=Path(directory))
            submit = ToolCall(
                id="submit", name="write_system_design", arguments={"system_design": valid_design()}
            )
            observation, design = execute_architect_tool(submit, state)
            self.assertFalse(observation.ok)
            self.assertIsNone(design)
            execute_architect_tool(ToolCall(id="read", name="read_artifact", arguments={}), state)
            observation, design = execute_architect_tool(submit, state)
            self.assertTrue(observation.ok)
            self.assertIsNotNone(design)

    def test_architect_rejects_incomplete_http_contract(self):
        with TemporaryDirectory(prefix="forgeai-role-contract-") as directory:
            state = ArchitectToolState(app_spec=architect_spec(), workspace_root=Path(directory))
            design = valid_design()
            design["interfaces"][0]["method"] = None
            observation, output = execute_architect_tool(
                ToolCall(
                    id="edit",
                    name="editor_write",
                    arguments={"system_design": design},
                ),
                state,
            )
            self.assertFalse(observation.ok)
            self.assertIsNone(output)
            self.assertIsNone(state.draft)

    def test_large_escaped_tool_output_stays_valid_json_and_explicitly_truncated(self):
        observation = ToolExecutionResult(
            tool_call_id="search",
            name="search_code",
            ok=True,
            summary="evidence",
            data={"hits": [{"text": '"\\\n' * 10000}] * 40},
            arguments={"query": "symbol"},
        )
        serialized = tool_observation_json(observation)
        payload = json.loads(serialized)
        self.assertLessEqual(len(serialized), MAX_TOOL_RESULT_CHARS)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["tool_call_id"], "search")
        self.assertFalse(observation.truncated)
