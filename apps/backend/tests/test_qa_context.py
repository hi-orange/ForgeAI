"""QA model receives a bounded projection while platform facts remain authoritative."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from qa_fixtures import add_unit_evidence, unit_manifest, unit_suite
from test_architect import valid_spec

from app.agents.qa_context import (
    qa_model_messages,
    qa_model_progress,
    unresolved_login_transition_test_ids,
)
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.schemas.qa_suite import QASuite, assertion_contract
from app.schemas.test_report import DefectRecord
from app.services.qa_assets import RUNNER_VERSION
from app.services.qa_verification import DefectResolution, QAVerificationState
from app.tools.test_engineer import execute_test_engineer_tool

SOURCE_HASH = "a" * 64


def state_for(root: Path) -> QAVerificationState:
    return QAVerificationState(
        app_spec=valid_spec(),
        code_item_id="ci_code",
        code_source_hash=SOURCE_HASH,
        workspace_root=root,
    )


def login_timeout_state() -> QAVerificationState:
    """Model four independent trusted timeouts after unsynchronized login routes."""

    state = state_for(Path("."))
    raw = unit_suite().model_dump(mode="json")
    base_case = raw["scenarios"][0]
    raw["scenarios"] = []
    raw["scripts"] = {}
    for test_id, route in (
        ("test_ac_profile", "/profile"),
        ("test_ac_my_applications", "/my-applications"),
        ("test_ac_employer_jobs", "/employer/jobs"),
        ("test_ac_applicants", "/applicants"),
    ):
        raw["scenarios"].append(
            {**base_case, "test_id": test_id, "environment": "isolated_browser"}
        )
        raw["scripts"][test_id] = {
            "steps": [
                {"kind": "browser", "action": "navigate", "path": "/login"},
                {
                    "kind": "browser",
                    "action": "fill",
                    "selector": "input[name=email]",
                    "value": "qa@example.test",
                },
                {
                    "kind": "browser",
                    "action": "fill",
                    "selector": "input[name=password]",
                    "value": "PRIVATE_PASSWORD",
                },
                {"kind": "browser", "action": "click", "selector": "button[type=submit]"},
                {"kind": "browser", "action": "navigate", "path": route},
                {"kind": "browser", "action": "expect_visible", "selector": "main"},
            ]
        }
    executed = QASuite.model_validate(raw)
    manifest = unit_manifest(executed, "failed")
    for row in manifest["results"]:
        row.update(
            failure_kind="product",
            evidence="TimeoutError: locator.waitFor: Timeout 15000ms exceeded. PRIVATE_PASSWORD",
            observations=[
                {"kind": "browser", "action": step.action}
                for step in executed.scripts[row["test_id"]].steps[:5]
            ],
        )
    state.suite_versions = [executed]
    state.executions = [manifest]
    return state


class QAContextTests(unittest.TestCase):
    def test_progress_shows_granted_execution_budget(self):
        state = state_for(Path("."))
        state.execution_extension_attempts = [9]
        state.executions = [{} for _ in range(6)]
        progress = qa_model_progress(state)
        self.assertEqual(progress["execution_budget"], 8)
        self.assertEqual(progress["remaining_execution_runs"], 2)

    @staticmethod
    def _tool_exchange(
        name: str,
        arguments: dict,
        *,
        ok: bool,
        summary: str,
        call_id: str = "mutation",
    ) -> list[dict]:
        return [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "PRIVATE_REASONING",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": json.dumps(
                    {
                        "tool_call_id": call_id,
                        "name": name,
                        "ok": ok,
                        "error_code": "TOOL_REJECTED" if not ok else None,
                        "summary": summary,
                        "data": {"raw_api_body": "PRIVATE_API_BODY"},
                        "arguments": arguments,
                    }
                ),
            },
        ]

    def test_resume_snapshot_omits_old_suites_raw_evidence_and_private_data(self):
        state = state_for(Path("."))
        base = unit_suite().model_dump(mode="json")
        base["scripts"]["test_ac_track"]["steps"][0]["json_body"] = {
            "email": "private@example.com",
            "access_token": "SECRET_ACCESS_TOKEN",
        }
        state.suite_versions = [
            QASuite.model_validate({**base, "revision": revision}) for revision in range(1, 13)
        ]
        manifest = unit_manifest(state.suite_versions[-1], "failed")
        manifest["runner_version"] = RUNNER_VERSION
        manifest["results"][0]["evidence"] = (
            "locator.waitFor strict mode violation private@example.com SECRET_ACCESS_TOKEN"
        )
        state.executions = [manifest, manifest]
        snapshot = qa_model_progress(state)
        serialized = json.dumps(snapshot)
        self.assertEqual(snapshot["latest_suite"]["suite_hash"], state.suite_versions[-1].digest())
        self.assertEqual(
            snapshot["latest_execution"]["results"][0]["diagnostic_category"],
            "non_unique_browser_locator",
        )
        self.assertLess(len(serialized), 8_000)
        self.assertNotIn("private@example.com", serialized)
        self.assertNotIn("SECRET_ACCESS_TOKEN", serialized)

    def test_progress_adds_source_bound_safe_failure_facts_and_active_defect_ids(self):
        state = state_for(Path("."))
        suite = unit_suite()
        state.suite_versions = [suite]
        manifest = unit_manifest(suite, "failed")
        manifest["results"][0].update(
            evidence=(
                "GET /api/records?token=SECRET_TOKEN: expected HTTP 200, actual 404 "
                "private@example.com"
            ),
            observations=[],
        )
        state.executions = [manifest]
        state.checks["all"] = ToolExecutionResult(
            tool_call_id="all",
            name="run_check",
            ok=False,
            error_code="CHECK_FAILED",
            summary="private@example.com",
            data={"output": "src/pages/JobsPage.tsx(42,8): error TS2322: SECRET_TOKEN"},
        )
        for defect_id in ("defect_open", "defect_fixed"):
            state.defects[defect_id] = DefectRecord(
                defect_id=defect_id,
                severity="major",
                title="问题",
                description="描述",
                evidence="private@example.com SECRET_TOKEN",
            )
        state.defect_resolutions["defect_fixed"] = DefectResolution(
            defect_id="defect_fixed",
            suite_hash=suite.digest(),
            execution_id="qa_unit_fixture",
            runner_version=RUNNER_VERSION,
        )
        progress = qa_model_progress(state)
        self.assertEqual(progress["active_defect_ids"], ["defect_open"])
        self.assertEqual(progress["resolved_defect_ids"], ["defect_fixed"])
        self.assertEqual(progress["resolvable_defect_ids"], [])
        self.assertNotIn("recorded_defect_ids", progress)
        facts = progress["failure_facts"]
        self.assertEqual(facts["nonpassed_total"], 1)
        self.assertEqual(facts["cases"][0]["expected_http_status"], 200)
        self.assertEqual(facts["checks"]["all"]["diagnostics"][0]["code"], "TS2322")
        self.assertNotIn("SECRET_TOKEN", json.dumps(facts))
        self.assertNotIn("private@example.com", json.dumps(facts))
        manifest["source_hash"] = "f" * 64
        stale = qa_model_progress(state)
        self.assertEqual(stale["failure_facts"]["cases"], [])
        manifest["source_hash"] = SOURCE_HASH
        manifest["code_item_id"] = "ci_other"
        stale = qa_model_progress(state)
        self.assertEqual(stale["failure_facts"]["cases"], [])

    def test_failure_snapshot_pinpoints_step_and_suggests_login_barrier(self):
        state = state_for(Path("."))
        raw = unit_suite().model_dump(mode="json")
        raw["scenarios"][0]["environment"] = "isolated_fullstack"
        raw["scripts"]["test_ac_track"]["steps"] = [
            {
                "kind": "api",
                "method": "GET",
                "path": "/api/records",
                "expect_status": 200,
                "assertions": [{"path": "$[0].title", "operator": "equals", "value": "book"}],
            },
            {"kind": "browser", "action": "navigate", "path": "/login"},
            {
                "kind": "browser",
                "action": "fill",
                "selector": "input[type=email]",
                "value": "a@b.test",
            },
            {
                "kind": "browser",
                "action": "fill",
                "selector": "input[name=password]",
                "value": "SECRET_ACCESS_TOKEN",
            },
            {"kind": "browser", "action": "click", "selector": "button[type=submit]"},
            {"kind": "browser", "action": "navigate", "path": "/profile"},
            {"kind": "browser", "action": "expect_visible", "selector": "h1"},
        ]
        suite = QASuite.model_validate(raw)
        state.suite_versions = [suite]
        manifest = unit_manifest(suite, "blocked")
        manifest["results"][0].update(
            failure_kind="script",
            evidence="Timeout during navigate private@example.com SECRET_ACCESS_TOKEN",
            observations=[{"kind": "api"}, *({"kind": "browser"} for _ in range(4))],
        )
        state.executions = [manifest]
        snapshot = qa_model_progress(state)
        row = snapshot["latest_execution"]["results"][0]
        self.assertEqual(row["failed_step_index"], 5)
        self.assertEqual(
            row["failed_step"], {"kind": "browser", "action": "navigate", "selector": None}
        )
        self.assertEqual(row["previous_step"], {"kind": "browser", "action": "click"})
        self.assertEqual(row["suggested_login_barrier_after_step_index"], 4)
        self.assertIn("wait_for_visible", snapshot["next_action_guidance"])
        self.assertIn("先安全适配并重跑", snapshot["next_action_guidance"])
        self.assertIn("勿将混合结果直接提交 failed", snapshot["next_action_guidance"])
        self.assertNotIn("job-card", snapshot["next_action_guidance"])
        serialized = json.dumps(snapshot)
        self.assertLess(len(serialized), 8_000)
        self.assertNotIn("private@example.com", serialized)
        self.assertNotIn("SECRET_ACCESS_TOKEN", serialized)

    def test_failure_step_index_requires_current_suite_and_valid_observation_count(self):
        state = state_for(Path("."))
        raw = unit_suite().model_dump(mode="json")
        raw["scenarios"][0]["environment"] = "isolated_browser"
        raw["scripts"]["test_ac_track"]["steps"] = [
            {"kind": "browser", "action": "navigate", "path": "/"},
            {"kind": "browser", "action": "click", "selector": "input[name=password]"},
            {"kind": "browser", "action": "expect_visible", "selector": "h1"},
        ]
        suite = QASuite.model_validate(raw)
        state.suite_versions = [suite]
        manifest = unit_manifest(suite, "blocked")
        manifest["results"][0].update(
            failure_kind="script",
            evidence="strict mode violation private@example.com",
            observations=[{"kind": "browser"}],
        )
        state.executions = [manifest]
        snapshot = qa_model_progress(state)
        row = snapshot["latest_execution"]["results"][0]
        self.assertEqual(row["failed_step_index"], 1)
        self.assertEqual(
            row["failed_step"],
            {
                "kind": "browser",
                "action": "click",
                "selector": "<redacted; inspect the specific script if needed>",
            },
        )
        self.assertEqual(row["previous_step"], {"kind": "browser", "action": "navigate"})
        self.assertIn("唯一 selector", snapshot["next_action_guidance"])
        self.assertNotIn("private@example.com", json.dumps(snapshot))
        manifest["results"][0]["observations"] = [{"kind": "browser"}] * 3
        self.assertNotIn(
            "failed_step_index", qa_model_progress(state)["latest_execution"]["results"][0]
        )
        manifest["results"][0]["observations"] = [{"kind": "browser"}]
        manifest["suite_hash"] = "other-suite"
        stale = qa_model_progress(state)
        self.assertNotIn("failed_step_index", stale["latest_execution"]["results"][0])
        self.assertIn("尚无独立执行证据", stale["next_action_guidance"])

    def test_changed_suite_still_requires_other_script_failures_to_be_adapted(self):
        state = state_for(Path("."))
        old_raw = unit_suite().model_dump(mode="json")
        base_case = old_raw["scenarios"][0]
        base_script = old_raw["scripts"]["test_ac_track"]
        for test_id in ("test_auth", "test_profile"):
            old_raw["scenarios"].append({**base_case, "test_id": test_id})
            old_raw["scripts"][test_id] = json.loads(json.dumps(base_script))
        executed = QASuite.model_validate(old_raw)
        manifest = unit_manifest(executed, "blocked")
        for row in manifest["results"]:
            row.update(
                failure_kind="script",
                evidence="strict mode violation",
                observations=[],
            )
        state.executions = [manifest]
        current_raw = executed.model_dump(mode="json")
        current_raw["revision"] = 2
        current_raw["scripts"]["test_ac_track"]["steps"][0]["path"] = "/api/records-v2"
        current = QASuite.model_validate(current_raw)
        state.suite_versions = [executed, current]

        snapshot = qa_model_progress(state)
        self.assertEqual(snapshot["unresolved_script_test_ids"], ["test_auth", "test_profile"])
        self.assertIn("test_auth", snapshot["next_action_guidance"])
        self.assertIn("test_profile", snapshot["next_action_guidance"])
        self.assertIn("全部已知脚本阻断处理后", snapshot["next_action_guidance"])
        self.assertIn("勿仅因 suite_hash 已变化就立即重跑", snapshot["next_action_guidance"])
        rows = {row["test_id"]: row for row in snapshot["latest_execution"]["results"]}
        self.assertNotIn("failed_step_index", rows["test_ac_track"])
        self.assertEqual(rows["test_auth"]["failed_step_index"], 0)

        repaired_raw = current.model_dump(mode="json")
        repaired_raw["revision"] = 3
        for test_id in ("test_auth", "test_profile"):
            repaired_raw["scripts"][test_id]["steps"][0]["path"] = f"/api/{test_id}-v2"
        state.suite_versions.append(QASuite.model_validate(repaired_raw))
        fully_repaired = qa_model_progress(state)
        self.assertEqual(fully_repaired["unresolved_script_test_ids"], [])
        self.assertIn("当前 suite_hash 尚无独立执行证据", fully_repaired["next_action_guidance"])

    def test_script_repair_guidance_requires_exact_source_runner_and_executed_suite(self):
        state = state_for(Path("."))
        suite = unit_suite()
        state.suite_versions = [suite]
        manifest = unit_manifest(suite, "blocked")
        manifest["results"][0].update(failure_kind="script", observations=[])
        state.executions = [manifest]
        self.assertEqual(qa_model_progress(state)["unresolved_script_test_ids"], ["test_ac_track"])
        for key, value in (
            ("source_hash", "b" * 64),
            ("code_item_id", "ci_other"),
            ("runner_version", "forgeai-qa-v1"),
            ("suite_hash", "c" * 64),
        ):
            with self.subTest(key=key):
                state.executions = [{**manifest, key: value}]
                self.assertEqual(qa_model_progress(state)["unresolved_script_test_ids"], [])

    def test_login_timeout_guard_waits_for_all_unchanged_transitions(self):
        state = login_timeout_state()
        executed = state.suite_versions[-1]
        current_raw = executed.model_dump(mode="json")
        current_raw["revision"] = 2
        profile_steps = current_raw["scripts"]["test_ac_profile"]["steps"]
        profile_steps.insert(
            4,
            {
                "kind": "browser",
                "action": "wait_for_visible",
                "selector": "[data-testid='header-identity']",
            },
        )
        # A revision and unrelated locator change cannot hide unchanged races.
        current_raw["scripts"]["test_ac_my_applications"]["steps"][1]["selector"] = (
            "input[data-testid=email]"
        )
        state.suite_versions.append(QASuite.model_validate(current_raw))
        remaining = [
            "test_ac_applicants",
            "test_ac_employer_jobs",
            "test_ac_my_applications",
        ]
        progress = qa_model_progress(state)
        self.assertEqual(progress["unresolved_login_transition_test_ids"], remaining)
        self.assertEqual(unresolved_login_transition_test_ids(state), remaining)
        self.assertIn("wait_for_visible", progress["next_action_guidance"])
        self.assertIn("test_ac_applicants", progress["next_action_guidance"])
        self.assertNotIn("PRIVATE_PASSWORD", json.dumps(progress))
        rows = {row["test_id"]: row for row in progress["latest_execution"]["results"]}
        self.assertEqual(
            rows["test_ac_applicants"]["suggested_login_barrier_after_step_index"],
            3,
        )

        call = ToolCall(id="premature", name="run_acceptance_tests", arguments={})
        with patch("app.tools.test_engineer.qa_runner.run_acceptance_tests") as runner:
            result, _ = execute_test_engineer_tool(call, state)
        self.assertFalse(result.ok)
        self.assertIn("test_ac_applicants", result.summary)
        self.assertEqual(len(state.executions), 1)
        runner.assert_not_called()

        repaired_raw = state.suite_versions[-1].model_dump(mode="json")
        repaired_raw["revision"] = 3
        for test_id in remaining:
            repaired_raw["scripts"][test_id]["steps"].insert(
                4,
                {
                    "kind": "browser",
                    "action": "wait_for_visible",
                    "selector": "[data-testid='header-identity']",
                },
            )
        state.suite_versions.append(QASuite.model_validate(repaired_raw))
        self.assertEqual(unresolved_login_transition_test_ids(state), [])
        passed = unit_manifest(state.suite_versions[-1], "passed")
        passed["execution_id"] = "qa_after_waits"
        with patch("app.tools.test_engineer.qa_runner.run_acceptance_tests") as runner:
            runner.return_value = ToolExecutionResult(
                tool_call_id="run",
                name="run_acceptance_tests",
                ok=True,
                summary="all passed",
                data={"manifest": passed},
            )
            result, _ = execute_test_engineer_tool(call, state)
        self.assertTrue(result.ok, result.summary)
        runner.assert_called_once()

    def test_login_timeout_guard_requires_exact_runner_evidence_and_first_assertion(self):
        state = login_timeout_state()
        manifest = state.executions[-1]
        current_raw = state.suite_versions[-1].model_dump(mode="json")
        current_raw["revision"] = 2
        state.suite_versions.append(QASuite.model_validate(current_raw))
        self.assertEqual(len(unresolved_login_transition_test_ids(state)), 4)
        changes = [
            ("runner_version", "forgeai-qa-v1"),
            ("source_hash", "b" * 64),
            ("code_item_id", "ci_unrelated"),
            ("suite_hash", "c" * 64),
        ]
        for key, value in changes:
            with self.subTest(identity=key):
                state.executions = [{**manifest, key: value}]
                self.assertEqual(unresolved_login_transition_test_ids(state), [])
        state.executions = [manifest]
        first = manifest["results"][0]
        for replacement in (
            {"failure_kind": "script", "status": "blocked"},
            {"evidence": "Error: expected visible but missing"},
            {"observations": [{"kind": "browser", "action": "navigate"}] * 5},
            {
                "observations": [
                    *first["observations"],
                    {"kind": "browser", "action": "expect_visible"},
                ]
            },
        ):
            with self.subTest(replacement=replacement):
                state.executions = [
                    {
                        **manifest,
                        "results": [{**first, **replacement}, *manifest["results"][1:]],
                    }
                ]
                self.assertNotIn("test_ac_profile", unresolved_login_transition_test_ids(state))

    def test_model_history_keeps_deduplicated_bounded_read_and_no_old_reasoning(self):
        state = state_for(Path("."))
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "initial"},
        ]
        for turn in range(40):
            history.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "OLD_SECRET_REASONING" * 1000,
                        "tool_calls": [
                            {
                                "id": f"r{turn}",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": f"r{turn}",
                        "name": "read_file",
                        "content": json.dumps(
                            {"summary": "read", "data": {"content": "x" * 10_000}}
                        ),
                    },
                ]
            )
        messages = qa_model_messages(
            system_message={"role": "system", "content": "system"},
            fixed_context={"acceptance_ids": ["ac_track"]},
            state=state,
            history=history,
        )
        serialized = json.dumps(messages)
        self.assertLess(len(serialized), 10_000)
        self.assertNotIn("OLD_SECRET_REASONING", serialized)
        latest_read = [message for message in messages if message["role"] == "tool"][-1]
        self.assertEqual(latest_read["tool_call_id"], "r39")
        self.assertTrue(json.loads(latest_read["content"])["truncated"])
        self.assertIn("勿重复调用相同工具参数", messages[-1]["content"])

    def test_model_history_keeps_unique_reads_across_batches_and_mutations(self):
        state = state_for(Path("."))
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "initial"}]

        def add_read(call_id: str, path: str, content: str) -> None:
            history.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "PRIVATE_REASONING",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": path}),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": "read_file",
                        "content": json.dumps(
                            {
                                "ok": True,
                                "summary": f"Read {path}",
                                "data": {
                                    "path": path,
                                    "content": content,
                                    "raw_api_body": "PRIVATE_API_BODY",
                                },
                            }
                        ),
                    },
                ]
            )

        add_read("a1", "frontend/a.tsx", "old version")
        add_read("b1", "frontend/b.tsx", "export const b = 1")
        add_read(
            "a2",
            "frontend/a.tsx",
            'export const a = 2; const password = "SECRET_ACCESS_TOKEN"; '
            'const email = "private@example.com"',
        )
        history.extend(
            self._tool_exchange(
                "adapt_test_script",
                {"test_id": "test_ac_track", "selector": "h1"},
                ok=False,
                summary="脚本步骤索引无效",
            )
        )
        messages = qa_model_messages(
            system_message={"role": "system", "content": "system"},
            fixed_context={},
            state=state,
            history=history,
        )
        read_results = [message for message in messages if message["role"] == "tool"]
        self.assertEqual([row["tool_call_id"] for row in read_results], ["b1", "a2"])
        self.assertIn("frontend/b.tsx", read_results[0]["content"])
        self.assertIn("export const a = 2", read_results[1]["content"])
        self.assertNotIn("old version", json.dumps(messages))
        self.assertNotIn("PRIVATE_REASONING", json.dumps(messages))
        self.assertNotIn("PRIVATE_API_BODY", json.dumps(messages))
        self.assertNotIn("SECRET_ACCESS_TOKEN", json.dumps(messages))
        self.assertNotIn("private@example.com", json.dumps(messages))
        self.assertEqual(
            json.loads(messages[1]["content"])["last_failed_mutation"]["summary"],
            "脚本步骤索引无效",
        )

    def test_model_history_read_memory_stays_below_total_budget(self):
        state = state_for(Path("."))
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "initial"}]
        for index in range(30):
            call_id = f"read_{index}"
            history.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": f"file_{index}.tsx"}),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": "read_file",
                        "content": json.dumps(
                            {
                                "ok": True,
                                "summary": "read",
                                "data": {"path": f"file_{index}.tsx", "content": "x" * 4_000},
                            }
                        ),
                    },
                ]
            )
        messages = qa_model_messages(
            system_message={"role": "system", "content": "system"},
            fixed_context={},
            state=state,
            history=history,
        )
        retained = [message for message in messages[2:] if message["role"] != "user"]
        self.assertLessEqual(
            sum(len(json.dumps(message, ensure_ascii=False)) for message in retained), 24_000
        )
        self.assertEqual(retained[-1]["tool_call_id"], "read_29")
        self.assertNotIn("file_0.tsx", json.dumps(retained))

    def test_initial_qa_keeps_bounded_frozen_app_spec_after_later_reads(self):
        state = state_for(Path("."))
        spec = state.app_spec.model_dump(mode="json")
        spec["goal"] = "招聘网站供求职者浏览和申请职位。private@example.com"
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "initial"}]
        history.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "spec",
                            "type": "function",
                            "function": {
                                "name": "read_artifact",
                                "arguments": '{"artifact":"app_spec"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "spec",
                    "name": "read_artifact",
                    "content": json.dumps(
                        {
                            "ok": True,
                            "summary": "已读取冻结成果：app_spec",
                            "data": {"app_spec": spec, "raw_api_body": "PRIVATE_API_BODY"},
                        }
                    ),
                },
            ]
        )
        history.extend(
            self._tool_exchange(
                "list_files",
                {"path": "."},
                ok=True,
                summary="列出 3 个文件",
                call_id="files",
            )
        )
        messages = qa_model_messages(
            system_message={"role": "system", "content": "system"},
            fixed_context={
                "acceptance_ids": [item.id for item in state.app_spec.acceptance_criteria]
            },
            state=state,
            history=history,
        )
        artifact = next(
            json.loads(message["content"])
            for message in messages
            if message.get("role") == "tool" and message.get("name") == "read_artifact"
        )
        self.assertIn("招聘网站供求职者浏览和申请职位", json.dumps(artifact, ensure_ascii=False))
        self.assertTrue(artifact["data"]["app_spec"]["features"])
        self.assertTrue(artifact["data"]["app_spec"]["acceptance_criteria"])
        self.assertNotIn("private@example.com", json.dumps(messages))
        self.assertNotIn("PRIVATE_API_BODY", json.dumps(messages))

    def test_search_cache_preserves_decorator_query_but_drops_private_search(self):
        state = state_for(Path("."))
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "initial"}]
        for call_id, query in (("decorator", "@router"), ("private", "private@example.com")):
            history.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "search_code",
                                    "arguments": json.dumps({"query": query}),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": "search_code",
                        "content": json.dumps(
                            {
                                "ok": True,
                                "summary": "命中 1 处",
                                "data": {
                                    "query": query,
                                    "hits": [
                                        {
                                            "path": "backend/routes.py",
                                            "line": 3,
                                            "text": '@router.get("/jobs")',
                                        }
                                    ],
                                },
                            }
                        ),
                    },
                ]
            )
        messages = qa_model_messages(
            system_message={"role": "system", "content": "system"},
            fixed_context={},
            state=state,
            history=history,
        )
        reads = [message for message in messages if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in reads], ["decorator"])
        self.assertIn("@router", json.dumps(messages))
        self.assertNotIn("private@example.com", json.dumps(messages))

    def test_failed_mutation_feedback_is_visible_without_private_arguments(self):
        state = state_for(Path("."))
        state.suite_versions = [unit_suite()]
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "initial"}]
        history.extend(
            self._tool_exchange(
                "adapt_test_script",
                {
                    "test_id": "test_ac_track",
                    "expected_suite_hash": "0" * 64,
                    "step_index": 2,
                    "selector": "input[value='private@example.com']",
                    "json_body": {"access_token": "SECRET_ACCESS_TOKEN"},
                },
                ok=False,
                summary="测试资产已更新；须提供准确 expected_suite_hash",
            )
        )
        messages = qa_model_messages(
            system_message={"role": "system", "content": "system"},
            fixed_context={},
            state=state,
            history=history,
        )
        feedback = json.loads(messages[1]["content"])["last_failed_mutation"]
        self.assertEqual(feedback["name"], "adapt_test_script")
        self.assertEqual(feedback["error_code"], "TOOL_REJECTED")
        self.assertEqual(feedback["summary"], "测试资产已更新；须提供准确 expected_suite_hash")
        self.assertEqual(
            feedback["arguments"],
            {
                "test_id": "test_ac_track",
                "expected_suite_hash_matches_latest": False,
                "step_index": 2,
                "edit_fields": ["selector"],
            },
        )
        serialized = json.dumps(messages)
        self.assertLess(len(serialized), 8_000)
        self.assertNotIn("private@example.com", serialized)
        self.assertNotIn("SECRET_ACCESS_TOKEN", serialized)
        self.assertNotIn("PRIVATE_API_BODY", serialized)
        self.assertNotIn("PRIVATE_REASONING", serialized)

    def test_failed_mutation_feedback_drops_after_success_and_redacts_unknown_error(self):
        state = state_for(Path("."))
        state.suite_versions = [unit_suite()]
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "initial"}]
        history.extend(
            self._tool_exchange(
                "adapt_test_script",
                {"test_id": "SECRET_ACCESS_TOKEN", "step_index": 1, "path": "/private?token=abc"},
                ok=False,
                summary="Validation failed for private@example.com SECRET_ACCESS_TOKEN",
            )
        )
        failed = qa_model_messages(
            system_message={"role": "system", "content": "system"},
            fixed_context={},
            state=state,
            history=history,
        )
        feedback = json.loads(failed[1]["content"])["last_failed_mutation"]
        self.assertEqual(feedback["arguments"], {"step_index": 1, "edit_fields": ["path"]})
        self.assertIn("请检查参数", feedback["summary"])
        self.assertNotIn("SECRET_ACCESS_TOKEN", json.dumps(failed))
        self.assertNotIn("private@example.com", json.dumps(failed))
        history.extend(
            self._tool_exchange(
                "adapt_test_script",
                {"test_id": "test_ac_track", "step_index": 1, "selector": "h1"},
                ok=True,
                summary="适配成功",
                call_id="success",
            )
        )
        recovered = qa_model_messages(
            system_message={"role": "system", "content": "system"},
            fixed_context={},
            state=state,
            history=history,
        )
        self.assertNotIn("last_failed_mutation", json.loads(recovered[1]["content"]))

    def test_adapt_rejections_preserve_specific_platform_guidance(self):
        state = state_for(Path("."))
        state.suite_versions = [unit_suite()]
        rejection_summaries = (
            "测试资产已更新；须提供准确 expected_suite_hash",
            "脚本步骤索引无效",
            "该步骤没有可修改的浏览器定位器",
            "不能删除操作或降低断言；请提交 test_challenges",
        )
        for summary in rejection_summaries:
            with self.subTest(summary=summary):
                history = [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "initial"},
                    *self._tool_exchange(
                        "adapt_test_script",
                        {"test_id": "test_ac_track", "step_index": 1, "selector": "h1"},
                        ok=False,
                        summary=summary,
                    ),
                ]
                messages = qa_model_messages(
                    system_message={"role": "system", "content": "system"},
                    fixed_context={},
                    state=state,
                    history=history,
                )
                self.assertEqual(
                    json.loads(messages[1]["content"])["last_failed_mutation"]["summary"],
                    summary,
                )

    def test_report_rejections_preserve_safe_gate_reason_and_identity_mismatch(self):
        state = state_for(Path("."))
        state.suite_versions = [unit_suite()]
        state.executions = [unit_manifest(state.suite_versions[-1])]
        summaries = (
            "质量结论没有引用当前 suite_hash",
            "质量结论没有引用当前测试执行",
            "验收失败时必须记录至少一个缺陷",
            "当前测试资产须使用最新独立执行器重新执行",
            "提交测试报告前必须生成桌面端与移动端截图",
        )
        for summary in summaries:
            with self.subTest(summary=summary):
                history = [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "initial"},
                    *self._tool_exchange(
                        "write_test_report",
                        {
                            "report_decision": {
                                "suite_hash": "0" * 64,
                                "execution_id": "qa_wrong",
                                "quality_conclusion": "failed",
                                "summary": "PRIVATE_REPORT_SUMMARY private@example.com",
                            }
                        },
                        ok=False,
                        summary=summary,
                    ),
                ]
                messages = qa_model_messages(
                    system_message={"role": "system", "content": "system"},
                    fixed_context={},
                    state=state,
                    history=history,
                )
                feedback = json.loads(messages[1]["content"])["last_failed_mutation"]
                self.assertEqual(feedback["summary"], summary)
                self.assertEqual(feedback["name"], "write_test_report")
                self.assertEqual(
                    feedback["arguments"],
                    {
                        "suite_hash_matches_latest": False,
                        "execution_id_matches_latest": False,
                        "quality_conclusion": "failed",
                    },
                )
                self.assertNotIn("PRIVATE_REPORT_SUMMARY", json.dumps(messages))
                self.assertNotIn("private@example.com", json.dumps(messages))

    def test_same_runner_source_suite_execution_is_not_restarted(self):
        state = state_for(Path("."))
        add_unit_evidence(state)
        state.executions[-1]["runner_version"] = RUNNER_VERSION
        call = ToolCall(id="again", name="run_acceptance_tests", arguments={})
        with patch("app.tools.test_engineer.qa_runner.run_acceptance_tests") as runner:
            result, _ = execute_test_engineer_tool(call, state)
        self.assertFalse(result.ok)
        self.assertIn("suite_hash", result.summary)
        runner.assert_not_called()
        self.assertEqual(len(state.executions), 1)

    def test_compact_report_attaches_exact_suite_and_runner_facts(self):
        state = state_for(Path("."))
        add_unit_evidence(state)
        state.executions[-1]["runner_version"] = RUNNER_VERSION
        state.checks["all"] = ToolExecutionResult(
            tool_call_id="all",
            name="run_check",
            ok=True,
            summary="all passed",
            data={"source_hash": SOURCE_HASH},
        )
        state.checks["visual"] = ToolExecutionResult(
            tool_call_id="visual",
            name="capture_screenshots",
            ok=True,
            summary="visual passed",
            data={
                "source_hash": SOURCE_HASH,
                "screenshots": [
                    {"path": "forgeai/evidence/desktop.png"},
                    {"path": "forgeai/evidence/mobile.png"},
                ],
            },
        )
        suite = state.suite_versions[-1]
        call = ToolCall(
            id="report",
            name="write_test_report",
            arguments={
                "report_decision": {
                    "suite_hash": suite.digest(),
                    "execution_id": state.executions[-1]["execution_id"],
                    "quality_conclusion": "passed",
                    "summary": "独立测试、完整检查和视觉检查通过。",
                }
            },
        )
        result, report = execute_test_engineer_tool(call, state)
        self.assertTrue(result.ok, result.summary)
        assert report is not None
        self.assertEqual(report.test_suite, suite)
        self.assertEqual(report.test_results[0].execution_id, state.executions[-1]["execution_id"])
        self.assertEqual(report.requirement_results[0].status.value, "passed")
        self.assertIn("desktop.png", report.check_results[1].evidence)

    def test_compact_report_rejects_stale_execution_identity(self):
        state = state_for(Path("."))
        add_unit_evidence(state)
        call = ToolCall(
            id="stale",
            name="write_test_report",
            arguments={
                "report_decision": {
                    "suite_hash": state.suite_versions[-1].digest(),
                    "execution_id": "qa_wrong",
                    "quality_conclusion": "passed",
                    "summary": "不可接受旧证据。",
                }
            },
        )
        result, report = execute_test_engineer_tool(call, state)
        self.assertFalse(result.ok)
        self.assertIsNone(report)

    def test_adapt_test_script_changes_selector_without_changing_assertions(self):
        with TemporaryDirectory() as directory:
            state = state_for(Path(directory))
            raw = unit_suite().model_dump(mode="json")
            raw["scenarios"][0]["environment"] = "isolated_fullstack"
            raw["scripts"]["test_ac_track"]["steps"] = [
                {"kind": "browser", "action": "navigate", "path": "/"},
                {"kind": "browser", "action": "click", "selector": "[data-testid='job-card']"},
                {"kind": "browser", "action": "expect_visible", "selector": "h1"},
            ]
            suite = QASuite.model_validate(raw)
            state.suite_versions = [suite]
            result, _ = execute_test_engineer_tool(
                ToolCall(
                    id="adapt",
                    name="adapt_test_script",
                    arguments={
                        "test_id": "test_ac_track",
                        "expected_suite_hash": suite.digest(),
                        "step_index": 1,
                        "selector": "[data-testid='job-card']:first-child",
                    },
                ),
                state,
            )
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(len(state.suite_versions), 2)
        updated = state.suite_versions[-1]
        self.assertEqual(result.data["suite_hash"], updated.digest())
        self.assertEqual(
            assertion_contract(updated.scripts["test_ac_track"]),
            assertion_contract(suite.scripts["test_ac_track"]),
        )

    def test_adapt_test_script_inserts_only_pure_browser_wait(self):
        state = state_for(Path("."))
        raw = unit_suite().model_dump(mode="json")
        raw["scenarios"][0]["environment"] = "isolated_fullstack"
        raw["scripts"]["test_ac_track"]["steps"] = [
            {"kind": "browser", "action": "navigate", "path": "/login"},
            {"kind": "browser", "action": "click", "selector": "button[type=submit]"},
            {"kind": "browser", "action": "navigate", "path": "/profile"},
            {"kind": "browser", "action": "expect_visible", "selector": "h1"},
        ]
        suite = QASuite.model_validate(raw)
        state.suite_versions = [suite]
        result, _ = execute_test_engineer_tool(
            ToolCall(
                id="wait",
                name="adapt_test_script",
                arguments={
                    "test_id": "test_ac_track",
                    "expected_suite_hash": suite.digest(),
                    "step_index": 1,
                    "insert_wait_for_visible_selector": "[data-testid='header-identity']",
                },
            ),
            state,
        )
        self.assertTrue(result.ok, result.summary)
        updated = state.suite_versions[-1].scripts["test_ac_track"]
        self.assertEqual(updated.steps[2].action, "wait_for_visible")
        self.assertEqual(
            assertion_contract(updated), assertion_contract(suite.scripts["test_ac_track"])
        )

    def test_compact_contract_challenge_keeps_frozen_case_ids(self):
        state = state_for(Path("."))
        result, report = execute_test_engineer_tool(
            ToolCall(
                id="challenge",
                name="write_test_report",
                arguments={
                    "report_decision": {
                        "quality_conclusion": "blocked",
                        "summary": "验收标准与产品意图有冲突，需先处理。",
                        "test_challenges": [
                            {
                                "challenge_id": "conflict_ac_track",
                                "test_ids": ["test_ac_track"],
                                "reason": "冻结预期与获批范围冲突。",
                                "requested_resolution": "确认冻结预期是否仍有效。",
                            }
                        ],
                    }
                },
            ),
            state,
        )
        self.assertTrue(result.ok, result.summary)
        assert report is not None
        self.assertEqual(report.test_results[0].test_id, "test_ac_track")
        self.assertEqual(report.test_results[0].status.value, "not_run")
