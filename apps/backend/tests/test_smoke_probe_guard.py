"""A failed public API probe cannot be rewritten into a false green check."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.generation.engineering.actions import prepare_action
from app.generation.engineering.checkpoint import new_checkpoint
from app.generation.engineering.smoke_guard import (
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
