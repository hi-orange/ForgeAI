"""Quality activity persistence for Test Engineer orchestration."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.orchestration import test_engineer as orch


class QualityActivityPersistenceTests(unittest.TestCase):
    def test_second_activity_write_marks_json_dirty(self) -> None:
        execution = MagicMock()
        execution.draft = None
        db = MagicMock()

        with patch("sqlalchemy.orm.attributes.flag_modified") as flag:
            orch._record_quality_activity(
                db,
                execution,
                name="quality_start",
                label="开始独立验证",
                detail="开始",
            )
            orch._record_quality_activity(
                db,
                execution,
                name="quality_error",
                label="独立验证失败",
                detail="Test Engineer 工具调用预算已用尽，尚未提交测试报告",
                ok=False,
            )

        activities = execution.draft["checkpoint"]["activity"]
        self.assertEqual(
            [item["name"] for item in activities],
            ["quality_start", "quality_error"],
        )
        self.assertFalse(activities[-1]["ok"])
        self.assertIn("预算已用尽", activities[-1]["detail"])
        self.assertEqual(flag.call_count, 2)
        self.assertEqual(db.commit.call_count, 2)
        self.assertEqual(db.refresh.call_count, 2)

    def test_quality_progress_is_persisted_without_losing_activity_and_renews_lease(self) -> None:
        execution = MagicMock()
        execution.task_id = "task_quality"
        execution.execution_id = "exec_quality"
        execution.draft = {
            "checkpoint": {
                "kind": "quality_checkpoint",
                "activity": [{"name": "quality_start"}],
            }
        }
        db = MagicMock()
        progress = {
            "schema_version": 1,
            "code_item_id": "ci_code",
            "source_hash": "a" * 64,
            "checks": {},
            "defects": [],
            "explore_before_check": 0,
        }

        with (
            patch("sqlalchemy.orm.attributes.flag_modified") as flag,
            patch("app.orchestration.test_engineer.renew_execution_lease") as renew,
        ):
            orch._save_quality_progress(db, execution, progress)

        checkpoint = execution.draft["checkpoint"]
        self.assertEqual(checkpoint["activity"], [{"name": "quality_start"}])
        self.assertEqual(checkpoint["verification"], progress)
        flag.assert_called_once_with(execution, "draft")
        db.flush.assert_called_once_with()
        renew.assert_called_once_with(db, "task_quality", "exec_quality")
        db.refresh.assert_called_once_with(execution)


if __name__ == "__main__":
    unittest.main()
