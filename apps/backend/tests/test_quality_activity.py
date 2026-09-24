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


if __name__ == "__main__":
    unittest.main()
