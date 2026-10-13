"""Engineering decisions and source writes without a model or database."""

import unittest
from copy import deepcopy

from app.core.exceptions import ConflictException
from app.generation.delivery import WorkItem
from app.generation.engineering.checkpoint import new_checkpoint
from app.generation.engineering.checks import apply_check_outcome
from app.schemas.agent_action import ToolExecutionResult


class CheckOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.item = WorkItem(id="feature", requirement_ids=["feature"], title="Feature")
        self.state = new_checkpoint()
        self.state.update(
            work_items=[self.item.model_dump(mode="json")],
            current_work_item_id=self.item.id,
        )

    def apply(self, result, *, max_repair_rounds=3):
        apply_check_outcome(
            self.state,
            work_item=self.item,
            result=result,
            max_repair_rounds=max_repair_rounds,
            check_runtime_version="test-runtime",
        )

    @staticmethod
    def result(*, ok=False, error_code="CHECK_FAILED", source_hash="source-v1"):
        return ToolExecutionResult(
            tool_call_id="check",
            name="run_check",
            ok=ok,
            error_code=None if ok else error_code,
            summary="check evidence",
            data={
                "source_hash": source_hash,
                "output": "feature.py: unknown name",
                "check_id": "all",
            },
        )

    def test_environment_failure_preserves_repair_budget_and_plan(self):
        self.state["repair_rounds"] = {self.item.id: 2}
        self.state["failure_streaks"] = {self.item.id: {"count": 2}}
        before = deepcopy(self.state)
        self.apply(self.result(error_code="CHECK_ENVIRONMENT_UNAVAILABLE"))
        self.assertEqual(self.state["outcome"], "blocked")
        self.assertEqual(self.state["blocked_reason_code"], "CHECK_ENVIRONMENT_UNAVAILABLE")
        for key in ("repair_rounds", "failure_streaks", "active_file_plan", "work_items"):
            self.assertEqual(self.state[key], before[key])

    def test_failed_check_preserves_exact_evidence_for_replanning(self):
        result = self.result()
        self.state["modified_files"] = ["backend/app/feature.py"]
        self.apply(result)
        repair = self.state["repair_context"]
        self.assertEqual(repair["failed_check"], result.model_dump(mode="json"))
        self.assertEqual(repair["modified_files"], self.state["modified_files"])
        self.assertEqual(repair["round"], 1)
        self.assertIsNone(self.state["active_file_plan"])
        self.assertEqual(self.state["outcome"], "running")

    def test_last_success_binds_generated_outcome_to_checked_source(self):
        self.apply(self.result(ok=True, source_hash="checked-source"))
        self.assertEqual(self.state["outcome"], "generated")
        self.assertEqual(self.state["completed_source_hash"], "checked-source")
        self.assertEqual(self.state["work_items"][0]["status"], "completed")
        self.assertIsNone(self.state["current_work_item_id"])

    def test_intermediate_success_does_not_claim_whole_app_generated(self):
        next_item = self.item.model_copy(update={"id": "next"})
        self.state["work_items"].append(next_item.model_dump(mode="json"))
        self.apply(self.result(ok=True))
        self.assertEqual(self.state["outcome"], "running")
        self.assertEqual(self.state["current_work_item_id"], "next")
        self.assertNotIn("completed_source_hash", self.state)

    def test_success_without_source_evidence_is_rejected(self):
        before = deepcopy(self.state)
        with self.assertRaisesRegex(ConflictException, "hash"):
            self.apply(self.result(ok=True, source_hash=None))
        self.assertEqual(self.state, before)
