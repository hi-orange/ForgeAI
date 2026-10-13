"""Synthetic runner facts for unit tests only, never integration evidence."""

from app.schemas.qa_suite import QASuite
from app.services.qa_assets import suite_test_results


def unit_suite():
    return QASuite.model_validate(
        {
            "revision": 1,
            "scenarios": [
                {
                    "test_id": "test_ac_track",
                    "acceptance_ids": ["ac_track"],
                    "objective": "保存后读取",
                    "preconditions": ["隔离应用已启动"],
                    "test_data": ["一条阅读记录"],
                    "steps": ["保存并读取列表"],
                    "expected_results": ["列表包含已保存记录"],
                    "category": "journey",
                    "environment": "isolated_api",
                    "approved_oracles": {"ac_track": "用户保存后可以在列表中看到记录"},
                }
            ],
            "scripts": {
                "test_ac_track": {
                    "steps": [
                        {
                            "kind": "api",
                            "path": "/api/records",
                            "expect_status": 200,
                            "assertions": [
                                {"path": "$[0].title", "operator": "equals", "value": "book"}
                            ],
                        }
                    ]
                }
            },
        }
    )


def unit_manifest(suite=None, status="passed", source_hash="a" * 64):
    suite = suite or unit_suite()
    return {
        "runner_version": "forgeai-qa-v1",
        "execution_id": "qa_unit_fixture",
        "code_item_id": "ci_code",
        "source_hash": source_hash,
        "suite_hash": suite.digest(),
        "results": [
            {
                "test_id": case.test_id,
                "status": status,
                "failure_kind": "product" if status == "failed" else None,
                "evidence": "unit fixture: actual assertion result",
                "observations": [{"kind": "api"}] * len(suite.scripts[case.test_id].steps),
            }
            for case in suite.scenarios
        ],
    }


def add_unit_evidence(state, status="passed"):
    suite = unit_suite()
    state.suite_versions = [suite]
    state.executions = [unit_manifest(suite, status, state.code_source_hash)]


def report_assets(status="passed"):
    suite = unit_suite()
    return {
        "test_suite": suite.model_dump(mode="json"),
        "suite_hash": suite.digest(),
        "test_results": suite_test_results(suite, unit_manifest(suite, status)),
        "regression_test_ids": ["test_ac_track"],
    }
