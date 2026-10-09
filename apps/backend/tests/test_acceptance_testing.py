import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import test_engineering_claim  # noqa: F401 - initialize engineering service facade first
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from test_architect import valid_spec

from app.core.exceptions import BusinessException
from app.generation.delivery import FileTask, ImplementationPlan
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.services.acceptance_testing import build_acceptance_test_plan
from app.tools.test_engineer import TestEngineerToolState, execute_test_engineer_tool


class AcceptanceTestingTests(unittest.TestCase):
    def test_migration_adds_and_removes_acceptance_plan_type(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "alembic/versions/1a2b3c4d5e6f_add_acceptance_test_plan.py"
        )
        spec = importlib.util.spec_from_file_location("acceptance_plan_migration", path)
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE configuration_item ("
                "id INTEGER PRIMARY KEY, semantic_type VARCHAR(32) NOT NULL, "
                "CONSTRAINT ck_configuration_item_semantic_type CHECK ("
                "semantic_type IN ('app_spec', 'system_design', 'code', 'test_report')))"
            )
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            connection.exec_driver_sql(
                "INSERT INTO configuration_item (id, semantic_type) "
                "VALUES (1, 'acceptance_test_plan')"
            )
            migration.downgrade()
            count = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM configuration_item"
            ).scalar_one()
        engine.dispose()
        self.assertEqual(count, 0)

    def test_plan_is_created_from_approved_acceptance_without_code(self):
        spec = valid_spec()
        source = SimpleNamespace(
            input_item=SimpleNamespace(item_id="ci_design"),
            app_spec=spec,
        )

        plan = build_acceptance_test_plan(source)

        self.assertEqual(plan.source_item_id, "ci_design")
        self.assertEqual(
            {acceptance_id for test in plan.tests for acceptance_id in test.acceptance_ids},
            {criterion.id for criterion in spec.acceptance_criteria},
        )
        self.assertTrue(all(test.target_path and test.command for test in plan.tests))
        self.assertTrue(all(test.target_path.startswith("platform://") for test in plan.tests))
        self.assertTrue(all("run_check" in test.command for test in plan.tests))
        self.assertEqual(plan.protected_paths, [])

    def test_code_engineer_cannot_plan_changes_to_frozen_test_assets(self):
        from app.orchestration.code_engineer import _normalize_plan

        with TemporaryDirectory(prefix="forgeai-protected-tests-") as directory:
            plan = ImplementationPlan(
                work_item_id="feature",
                summary="try to weaken acceptance",
                files=[
                    FileTask(
                        id="weaken_test",
                        path="backend/tests/acceptance/test_contract.py",
                        description="remove failing assertion",
                    )
                ],
            )

            with self.assertRaisesRegex(BusinessException, "不能修改平台冻结"):
                _normalize_plan(Path(directory), plan)

    def test_report_is_bound_to_frozen_test_hash_and_test_ids(self):
        spec = valid_spec()
        plan = build_acceptance_test_plan(
            SimpleNamespace(input_item=SimpleNamespace(item_id="ci_spec"), app_spec=spec)
        )
        source_hash = "a" * 64
        test_hash = "b" * 64
        with TemporaryDirectory(prefix="forgeai-test-evidence-") as directory:
            state = TestEngineerToolState(
                app_spec=spec,
                code_item_id="ci_code",
                code_source_hash=source_hash,
                workspace_root=Path(directory),
                acceptance_test_plan_item_id="ci_tests",
                acceptance_test_hash=test_hash,
                acceptance_test_plan=plan,
            )
            state.checks["all"] = ToolExecutionResult(
                tool_call_id="all",
                name="run_check",
                ok=True,
                summary="all passed",
                data={"source_hash": source_hash},
            )
            state.checks["visual"] = ToolExecutionResult(
                tool_call_id="visual",
                name="capture_screenshots",
                ok=True,
                summary="visual passed",
                data={
                    "source_hash": source_hash,
                    "screenshots": [
                        {"path": "forgeai/evidence/hash/desktop.png"},
                        {"path": "forgeai/evidence/hash/mobile.png"},
                    ],
                },
            )
            requirement_results = [
                {
                    "requirement_id": criterion.id,
                    "status": "passed",
                    "evidence": f"observed {criterion.id}",
                }
                for criterion in spec.acceptance_criteria
            ]
            report_payload = {
                "code_item_id": "ci_code",
                "source_hash": source_hash,
                "requirement_results": requirement_results,
                "check_results": [
                    {"check_id": "all", "status": "passed", "evidence": "all passed"},
                    {
                        "check_id": "visual",
                        "status": "passed",
                        "evidence": (
                            "forgeai/evidence/hash/desktop.png forgeai/evidence/hash/mobile.png"
                        ),
                    },
                ],
                "defects": [],
                "quality_conclusion": "passed",
                "summary": "frozen acceptance passed",
                "residual_risks": [],
            }

            _, report = execute_test_engineer_tool(
                ToolCall(
                    id="report",
                    name="write_test_report",
                    arguments={"test_report": report_payload},
                ),
                state,
            )

        assert report is not None
        self.assertEqual((report.test_plan_item_id, report.test_hash), ("ci_tests", test_hash))
        self.assertEqual(
            {item.test_id for item in report.test_results},
            {item.test_id for item in plan.tests},
        )
        self.assertEqual(set(report.regression_test_ids), {item.test_id for item in plan.tests})


if __name__ == "__main__":
    unittest.main()
