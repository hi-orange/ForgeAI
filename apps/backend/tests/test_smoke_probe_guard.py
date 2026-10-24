"""A failed public API probe cannot be rewritten into a false green check."""

import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from app.generation.engineering.actions import PreparedAction, commit_action, prepare_action
from app.generation.engineering.checkpoint import new_checkpoint
from app.generation.engineering.smoke_guard import (
    ensure_smoke_probe_baseline,
    guard_passing_smoke_check,
    hydrate_legacy_failed_api_probes,
    protected_api_probes,
    record_failed_api_probes,
    smoke_probe_violation,
)
from app.generation.workspace import create_workspace
from app.orchestration.code_engineer import _EngineeringNodes
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.tools.paths import sha256_bytes


def failed_public_probe(probe_id: str, route: str = "/api/v1/jobs?q=test") -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_call_id="check",
        name="run_check",
        ok=False,
        error_code="CHECK_FAILED",
        summary="API smoke failed",
        data={
            "check_id": "all",
            "source_hash": "source-before",
            "output": (
                f'INFO: "GET {route} HTTP/1.1" 404 Not Found\n'
                f"RuntimeError: API 联调失败 {probe_id}："
                '期望 HTTP 200，实际 404；{"detail":"Not Found"}'
            ),
        },
    )


class SmokeProbeGuardTests(unittest.TestCase):
    def setUp(self):
        self.root = (
            Path(self.enterContext(TemporaryDirectory(prefix="forgeai-smoke-guard-"))) / "workspace"
        )
        create_workspace(self.root)
        self.path = self.root / "forgeai.smoke.json"
        self.manifest = json.loads(self.path.read_text(encoding="utf-8"))
        self.manifest["api_checks"].extend(
            [
                {
                    "id": "public-list-shows-new-job",
                    "method": "GET",
                    "path": "/api/v1/jobs?q=test",
                    "expect_status": 200,
                    "expect_json_paths": ["code", "data.items"],
                },
                {
                    "id": "public-list-hides-closed-job",
                    "method": "GET",
                    "path": "/api/v1/jobs?q=test",
                    "expect_status": 200,
                    "expect_json_paths": ["code", "data.items"],
                },
            ]
        )
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def _inherited_checkpoint(self):
        checkpoint = new_checkpoint()
        checkpoint["model_turns"] = 171  # A resumed legacy checkpoint has no new ledger yet.
        ensure_smoke_probe_baseline(checkpoint, self.path)
        return checkpoint

    def _staged_manifest(self, checkpoint, manifest):
        original = self.path.read_bytes()
        return prepare_action(
            ToolCall(
                id="write-smoke",
                name="write_files",
                arguments={
                    "writes": [
                        {
                            "path": "forgeai.smoke.json",
                            "content": json.dumps(manifest),
                            "expected_hash": sha256_bytes(original),
                        }
                    ]
                },
            ),
            self.root,
            {"forgeai.smoke.json": sha256_bytes(original)},
            checkpoint=checkpoint,
        )

    def test_resumed_checkpoint_rejects_bulk_deletion_before_visual_schema(self):
        checkpoint = self._inherited_checkpoint()
        baseline = deepcopy(checkpoint["smoke_probe_baseline_v1"])
        original = self.path.read_bytes()
        candidate = deepcopy(self.manifest)
        candidate["api_checks"] = candidate["api_checks"][:1]
        candidate["browser_checks"] = []
        candidate["visual_contract"]["theme_mode"] = "invalid"
        action = self._staged_manifest(checkpoint, candidate)
        self.assertFalse(action.result.ok)
        self.assertIn("2 个 API", action.result.summary)
        self.assertIn("1 个浏览器", action.result.summary)
        self.assertIn("edit_file_by_replace", action.result.summary)
        self.assertNotIn("theme_mode", action.result.summary)
        self.assertFalse(action.updates)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(checkpoint["smoke_probe_baseline_v1"], baseline)

    def test_unverified_additions_can_be_corrected_or_removed_but_old_ids_remain(self):
        checkpoint = self._inherited_checkpoint()
        expanded = deepcopy(self.manifest)
        expanded["api_checks"].insert(
            0,
            {"id": "live-setup", "path": "/api/v1/jobs", "expect_status": 200},
        )
        new_browser = deepcopy(expanded["browser_checks"][0])
        new_browser["id"] = "live-browser"
        expanded["browser_checks"].append(new_browser)
        first = self._staged_manifest(checkpoint, expanded)
        self.assertTrue(first.result.ok, first.result.summary)
        self.assertTrue(commit_action(first, self.root, revision_id="test-revision").ok)
        failed_all = ToolExecutionResult(
            tool_call_id="all",
            name="run_check",
            ok=False,
            error_code="CHECK_FAILED",
            summary="new probe failed",
            data={"check_id": "all"},
        )
        self.assertFalse(guard_passing_smoke_check(checkpoint, failed_all, self.path).ok)
        self.assertNotIn("live-setup", checkpoint["smoke_probe_baseline_v1"]["api_checks"])

        corrected = deepcopy(expanded)
        corrected["api_checks"][0]["path"] = "/api/v1/companies/999999"
        corrected["api_checks"][0]["expect_status"] = 404
        corrected["browser_checks"][-1]["route"] = "/login"
        corrected["browser_checks"][-1]["expect_api"] = ["/api/v1/jobs"]
        second = self._staged_manifest(checkpoint, corrected)
        self.assertTrue(second.result.ok, second.result.summary)
        self.assertTrue(commit_action(second, self.root, revision_id="test-correction").ok)

        reduced = deepcopy(corrected)
        reduced["api_checks"] = [
            check for check in reduced["api_checks"] if check["id"] != "live-setup"
        ]
        reduced["browser_checks"] = [
            check for check in reduced["browser_checks"] if check["id"] != "live-browser"
        ]
        third = self._staged_manifest(checkpoint, reduced)
        self.assertTrue(third.result.ok, third.result.summary)
        self.assertTrue(commit_action(third, self.root, revision_id="test-withdrawal").ok)

        old_removed = deepcopy(reduced)
        old_removed["api_checks"] = [
            check
            for check in old_removed["api_checks"]
            if check["id"] != "public-list-shows-new-job"
        ]
        blocked = self._staged_manifest(checkpoint, old_removed)
        self.assertFalse(blocked.result.ok)
        self.assertIn("public-list-shows-new-job", blocked.result.summary)

    def test_passing_all_freezes_added_probe_for_later_writes(self):
        checkpoint = self._inherited_checkpoint()
        expanded = deepcopy(self.manifest)
        expanded["api_checks"].insert(
            0,
            {"id": "live-setup", "path": "/api/v1/jobs", "expect_status": 200},
        )
        first = self._staged_manifest(checkpoint, expanded)
        self.assertTrue(first.result.ok, first.result.summary)
        self.assertTrue(commit_action(first, self.root, revision_id="test-revision").ok)
        passed = ToolExecutionResult(
            tool_call_id="all",
            name="run_check",
            ok=True,
            summary="all checks passed",
            data={"check_id": "all", "source_hash": "source-after"},
        )
        self.assertTrue(guard_passing_smoke_check(checkpoint, passed, self.path).ok)
        self.assertIn("live-setup", checkpoint["smoke_probe_baseline_v1"]["api_checks"])
        reduced = deepcopy(expanded)
        reduced["api_checks"] = [
            check for check in reduced["api_checks"] if check["id"] != "live-setup"
        ]
        blocked = self._staged_manifest(checkpoint, reduced)
        self.assertFalse(blocked.result.ok)
        self.assertIn("live-setup", blocked.result.summary)
        self.path.write_text(json.dumps(reduced), encoding="utf-8")
        self.assertFalse(guard_passing_smoke_check(checkpoint, passed, self.path).ok)

    def test_baseline_preserves_api_and_browser_observations(self):
        checkpoint = self._inherited_checkpoint()
        changed = deepcopy(self.manifest)
        changed["api_checks"][0]["expect_json_paths"] = ["code"]
        self.assertIn("响应字段断言", smoke_probe_violation(checkpoint, changed))
        changed = deepcopy(self.manifest)
        changed["api_checks"][0]["path"] = "/api/v1/private/health"
        self.assertIn("请求路径", smoke_probe_violation(checkpoint, changed))
        changed = deepcopy(self.manifest)
        changed["browser_checks"][0]["expect_api"] = []
        self.assertIn("前后端请求断言", smoke_probe_violation(checkpoint, changed))
        changed = deepcopy(self.manifest)
        changed["browser_checks"][0]["actions"] = []
        self.assertIn("可观察断言", smoke_probe_violation(checkpoint, changed))

    def test_negative_api_request_input_remains_strict(self):
        manifest = deepcopy(self.manifest)
        manifest["api_checks"].append(
            {
                "id": "invalid-salary",
                "method": "POST",
                "path": "/api/v1/employer/jobs",
                "json": {"salary_min": 9000, "salary_max": 1000},
                "expect_status": 422,
            }
        )
        self.path.write_text(json.dumps(manifest), encoding="utf-8")
        checkpoint = self._inherited_checkpoint()
        changed = deepcopy(manifest)
        changed["api_checks"][-1]["json"]["salary_min"] = 1000
        self.assertIn("负向请求数据", smoke_probe_violation(checkpoint, changed))
        changed = deepcopy(manifest)
        changed["api_checks"][-1]["expect_status"] = 201
        self.assertIn("预期状态码", smoke_probe_violation(checkpoint, changed))

    def test_live_fixture_reordering_capture_rename_and_text_update_are_legal(self):
        original = deepcopy(self.manifest)
        original["api_checks"][1]["capture"] = {"old_job_id": "data.items.0.id"}
        original["api_checks"][2]["path"] = "/api/v1/jobs/${old_job_id}"
        original["browser_checks"][0]["route"] = "/jobs/1"
        original["browser_checks"][0]["expect_api"] = [
            {"method": "GET", "path": "/api/v1/jobs/1", "status": 200}
        ]
        self.path.write_text(json.dumps(original), encoding="utf-8")
        checkpoint = self._inherited_checkpoint()
        updated = deepcopy(original)
        updated["api_checks"][1]["capture"] = {"new_job_id": "data.items.0.id"}
        updated["api_checks"][2]["path"] = "/api/v1/jobs/${new_job_id}"
        updated["browser_checks"][0]["route"] = "/jobs/${new_job_id}"
        updated["browser_checks"][0]["expect_api"][0]["path"] = "/api/v1/jobs/${new_job_id}"
        updated["browser_checks"][0]["expect_text"] = ["现场发布职位", "真实公司"]
        updated["browser_checks"][0]["actions"][1]["text"] = "现场发布职位"
        updated["api_checks"] = updated["api_checks"][1:] + updated["api_checks"][:1]
        updated["browser_checks"].append(
            {
                "id": "empty-before-live-setup",
                "route": "/",
                "expect_api": ["/api/v1/jobs"],
                "actions": [{"type": "expect_text", "text": "暂无职位"}],
            }
        )
        self.assertIsNone(smoke_probe_violation(checkpoint, updated))
        self.assertTrue(self._staged_manifest(checkpoint, updated).result.ok)

    def test_missing_manifest_cannot_turn_a_passing_all_green(self):
        checkpoint = self._inherited_checkpoint()
        self.path.unlink()
        passed = ToolExecutionResult(
            tool_call_id="all",
            name="run_check",
            ok=True,
            summary="all checks passed",
            data={"check_id": "all", "source_hash": "source-after"},
        )
        guarded = guard_passing_smoke_check(checkpoint, passed, self.path)
        self.assertFalse(guarded.ok)
        self.assertEqual(guarded.error_code, "SMOKE_PROBE_WEAKENED")

    def test_commit_refuses_deleting_required_smoke_manifest(self):
        original = self.path.read_bytes()
        action = PreparedAction(
            result=ToolExecutionResult(
                tool_call_id="delete",
                name="delete_file",
                ok=True,
                summary="deleted",
            ),
            originals={"forgeai.smoke.json": original},
            deletions={"forgeai.smoke.json"},
        )
        result = commit_action(action, self.root, revision_id="test-revision")
        self.assertFalse(result.ok)
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_inherited_manifest_does_not_install_a_baseline(self):
        invalid = deepcopy(self.manifest)
        invalid["api_checks"][0].pop("id")
        self.path.write_text(json.dumps(invalid), encoding="utf-8")
        checkpoint = new_checkpoint()
        with self.assertRaises(ValueError):
            ensure_smoke_probe_baseline(checkpoint, self.path)
        self.assertNotIn("smoke_probe_baseline_v1", checkpoint)

        repaired = self._staged_manifest(checkpoint, self.manifest)
        self.assertTrue(repaired.result.ok, repaired.result.summary)
        self.assertTrue(commit_action(repaired, self.root, revision_id="repair-manifest").ok)
        ensure_smoke_probe_baseline(checkpoint, self.path)
        self.assertEqual(
            set(checkpoint["smoke_probe_baseline_v1"]["api_checks"]),
            {"health", "public-list-shows-new-job", "public-list-hides-closed-job"},
        )

    def test_legacy_failure_history_rejects_removed_and_private_replacement(self):
        checkpoint = new_checkpoint()
        checkpoint["diagnostic_history"] = [
            {
                "evidence_id": f"legacy-{turn}",
                "scope": "check:all",
                "model_turn": turn,
                "result": failed_public_probe(probe).model_dump(mode="json"),
            }
            for turn, probe in enumerate(
                ("public-list-shows-new-job", "public-list-hides-closed-job"), 1
            )
        ]
        modified = json.loads(json.dumps(self.manifest))
        modified["api_checks"] = [
            check for check in modified["api_checks"] if check["id"] != "public-list-shows-new-job"
        ]
        modified["api_checks"][-1]["path"] = "/api/v1/employer/jobs"
        first = smoke_probe_violation(checkpoint, modified)
        self.assertIn("public-list-shows-new-job", first)
        modified["api_checks"].append(self.manifest["api_checks"][-2])
        second = smoke_probe_violation(checkpoint, modified)
        self.assertIn("public-list-hides-closed-job", second)
        self.assertIn("/api/v1/jobs", second)

        self.path.write_text(json.dumps(modified), encoding="utf-8")
        passed = ToolExecutionResult(
            tool_call_id="all",
            name="run_check",
            ok=True,
            summary="all checks passed",
            data={"check_id": "all", "source_hash": "source-after"},
        )
        guarded = guard_passing_smoke_check(checkpoint, passed, self.path)
        self.assertFalse(guarded.ok)
        self.assertEqual(guarded.error_code, "SMOKE_PROBE_WEAKENED")
        self.assertEqual(guarded.data["source_hash"], "source-after")
        completion_result = _EngineeringNodes._remember_check(
            checkpoint, "all", passed, root=self.root
        )
        self.assertFalse(completion_result.ok)
        self.assertEqual(completion_result.error_code, "SMOKE_PROBE_WEAKENED")

    def test_legacy_request_keeps_query_and_cannot_be_redefined_by_later_failure(self):
        checkpoint = new_checkpoint()
        checkpoint["diagnostic_history"] = [
            {
                "scope": "check:all",
                "result": failed_public_probe("public-list-shows-new-job").model_dump(mode="json"),
            }
        ]
        edited = json.loads(json.dumps(self.manifest))
        edited["api_checks"][-2]["path"] = "/api/v1/jobs"
        self.assertIn("查询参数", smoke_probe_violation(checkpoint, edited))
        edited["api_checks"][-2]["path"] = "/api/v1/employer/jobs"
        self.path.write_text(json.dumps(edited), encoding="utf-8")
        record_failed_api_probes(
            checkpoint, failed_public_probe("public-list-shows-new-job"), self.path
        )
        # Even a persisted ledger from a later, weakened manifest cannot
        # replace the earlier request captured in the check history.
        checkpoint["smoke_failure_probes"] = {
            "public-list-shows-new-job": {
                "id": "public-list-shows-new-job",
                "method": "GET",
                "path": "/api/v1/employer/jobs",
                "query_keys": [],
                "expect_status": 200,
            }
        }
        self.assertIn("/api/v1/jobs", smoke_probe_violation(checkpoint, edited))

    def test_legacy_failure_freezes_query_values_with_order_and_encoding_normalized(self):
        checkpoint = new_checkpoint()
        checkpoint["diagnostic_history"] = [
            {
                "scope": "check:all",
                "result": failed_public_probe(
                    "public-list-shows-new-job",
                    "/api/v1/jobs?q=hello%20world&sort=new&sort=recent",
                ).model_dump(mode="json"),
            }
        ]
        updated = json.loads(json.dumps(self.manifest))
        check = updated["api_checks"][-2]
        check["path"] = "/api/v1/jobs?sort=recent&q=hello+world&sort=new"
        self.assertIsNone(smoke_probe_violation(checkpoint, updated))
        for weakened in (
            "/api/v1/jobs?sort=recent&q=other&sort=new",
            "/api/v1/jobs?sort=recent&q=&sort=new",
            "/api/v1/jobs?sort=recent&q&sort=new",
            "/api/v1/jobs?sort=recent&q=hello+world&sort=recent",
        ):
            with self.subTest(weakened=weakened):
                check["path"] = weakened
                self.assertIn("查询参数", smoke_probe_violation(checkpoint, updated))

    def test_recorded_failure_freezes_literal_query_value(self):
        checkpoint = new_checkpoint()
        record_failed_api_probes(
            checkpoint, failed_public_probe("public-list-shows-new-job"), self.path
        )
        baseline = checkpoint["smoke_failure_probes"]["public-list-shows-new-job"]
        self.assertEqual(baseline["query_params"], [["q", "test"]])
        updated = json.loads(json.dumps(self.manifest))
        updated["api_checks"][-2]["path"] = "/api/v1/jobs?q=other"
        self.assertIn("查询参数", smoke_probe_violation(checkpoint, updated))

    def test_recording_does_not_adopt_a_manifest_with_different_literal_value(self):
        checkpoint = new_checkpoint()
        checkpoint["diagnostic_history"] = [
            {
                "scope": "check:all",
                "result": failed_public_probe("public-list-shows-new-job").model_dump(mode="json"),
            }
        ]
        changed = json.loads(json.dumps(self.manifest))
        changed["api_checks"][-2]["path"] = "/api/v1/jobs?q=other"
        self.path.write_text(json.dumps(changed), encoding="utf-8")
        record_failed_api_probes(
            checkpoint, failed_public_probe("public-list-shows-new-job"), self.path
        )
        self.assertNotIn("public-list-shows-new-job", checkpoint["smoke_failure_probes"])
        self.assertIn("查询参数", smoke_probe_violation(checkpoint, changed))

    def test_captured_query_value_keeps_template_and_other_literals(self):
        checkpoint = new_checkpoint()
        check = self.manifest["api_checks"][-2]
        check["path"] = "/api/v1/jobs?q=${search_term}&sort=recent"
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
        failed = failed_public_probe("public-list-shows-new-job", "/api/v1/jobs?sort=recent&q=test")
        record_failed_api_probes(checkpoint, failed, self.path)
        baseline = checkpoint["smoke_failure_probes"]["public-list-shows-new-job"]
        self.assertEqual(baseline["query_params"], [["q", "test"], ["sort", "recent"]])
        self.assertEqual(
            baseline["manifest_query_params"],
            [["q", "${search_term}"], ["sort", "recent"]],
        )
        check["path"] = "/api/v1/jobs?sort=recent&q=${search_term}"
        self.assertIsNone(smoke_probe_violation(checkpoint, self.manifest))
        check["path"] = "/api/v1/jobs?sort=old&q=${search_term}"
        self.assertIn("查询参数", smoke_probe_violation(checkpoint, self.manifest))
        check["path"] = "/api/v1/jobs?sort=recent&q=${other_term}"
        self.assertIn("查询参数", smoke_probe_violation(checkpoint, self.manifest))

    def test_captured_value_cannot_cover_a_changed_literal_with_same_key(self):
        checkpoint = new_checkpoint()
        check = self.manifest["api_checks"][-2]
        check["path"] = "/api/v1/jobs?q=${search_term}&q=other"
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
        record_failed_api_probes(
            checkpoint,
            failed_public_probe("public-list-shows-new-job", "/api/v1/jobs?q=test&q=recent"),
            self.path,
        )
        self.assertNotIn("public-list-shows-new-job", checkpoint["smoke_failure_probes"])

    def test_legacy_history_is_scoped_to_current_item_then_carried_forward(self):
        checkpoint = new_checkpoint()
        checkpoint["current_work_item_id"] = "current"
        checkpoint["work_items"] = [
            {"id": "previous", "status": "completed"},
            {"id": "current", "status": "pending"},
        ]
        checkpoint["plan_history"] = [
            {
                "plan": {"work_item_id": "previous"},
                "check": {"model_turn": 5},
            }
        ]
        checkpoint["diagnostic_history"] = [
            {
                "scope": "check:all",
                "model_turn": turn,
                "result": failed_public_probe(probe).model_dump(mode="json"),
            }
            for turn, probe in (
                (4, "old-unrelated-probe"),
                (8, "public-list-shows-new-job"),
            )
        ]
        self.assertEqual(set(protected_api_probes(checkpoint)), {"public-list-shows-new-job"})
        hydrate_legacy_failed_api_probes(checkpoint)
        checkpoint["work_items"][1]["status"] = "completed"
        checkpoint["plan_history"].append(
            {"plan": {"work_item_id": "current"}, "check": {"model_turn": 10}}
        )
        self.assertEqual(set(protected_api_probes(checkpoint)), {"public-list-shows-new-job"})

    def test_new_failure_freezes_assertions_but_allows_additive_and_token_fixes(self):
        checkpoint = new_checkpoint()
        check = self.manifest["api_checks"][-1]
        check["headers"] = {"Authorization": "Bearer ${old_token}"}
        check["capture"] = {"job_id": "data.items.0.id"}
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
        record_failed_api_probes(
            checkpoint, failed_public_probe("public-list-hides-closed-job"), self.path
        )
        self.assertEqual(
            checkpoint["smoke_failure_probes"]["public-list-hides-closed-job"]["expect_json_paths"],
            ["code", "data.items"],
        )
        updated = json.loads(json.dumps(self.manifest))
        current = updated["api_checks"][-1]
        current["headers"]["Authorization"] = "Bearer ${valid_token}"
        current["expect_json_paths"].append("data.total")
        self.assertIsNone(smoke_probe_violation(checkpoint, updated))
        current["expect_json_paths"] = ["code"]
        self.assertIn("字段断言", smoke_probe_violation(checkpoint, updated))
        current["expect_json_paths"] = ["code", "data.items"]
        current.pop("capture")
        self.assertIn("变量捕获", smoke_probe_violation(checkpoint, updated))

    def test_original_public_request_cannot_acquire_authorization(self):
        checkpoint = new_checkpoint()
        record_failed_api_probes(
            checkpoint, failed_public_probe("public-list-shows-new-job"), self.path
        )
        updated = json.loads(json.dumps(self.manifest))
        updated["api_checks"][-2]["headers"] = {"Authorization": "Bearer ${employer_token}"}
        self.assertIn("需登录", smoke_probe_violation(checkpoint, updated))

    def test_staged_batch_rejects_weakened_manifest_without_committing_other_files(self):
        checkpoint = new_checkpoint()
        checkpoint["diagnostic_history"] = [
            {
                "scope": "check:all",
                "result": failed_public_probe("public-list-hides-closed-job").model_dump(
                    mode="json"
                ),
            }
        ]
        original = self.path.read_bytes()
        altered = json.loads(original)
        altered["api_checks"][-1]["path"] = "/api/v1/employer/jobs"
        action = prepare_action(
            ToolCall(
                id="write",
                name="write_files",
                arguments={
                    "writes": [
                        {"path": "backend/app/new.py", "content": "VALUE = 1\n"},
                        {
                            "path": "forgeai.smoke.json",
                            "content": json.dumps(altered),
                            "expected_hash": sha256_bytes(original),
                        },
                    ]
                },
            ),
            self.root,
            {"forgeai.smoke.json": sha256_bytes(original)},
            checkpoint=checkpoint,
        )
        self.assertFalse(action.result.ok)
        self.assertFalse(action.updates)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "backend/app/new.py").exists())


if __name__ == "__main__":
    unittest.main()
