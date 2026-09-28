"""Regression coverage for the activity-history query shape."""

from collections import namedtuple
from unittest import TestCase
from unittest.mock import MagicMock

from sqlalchemy.dialects import mysql

from app.services.requirements import _activities_for_run


class RequirementsActivityQueryTests(TestCase):
    def test_large_drafts_are_fetched_outside_the_ordered_query(self) -> None:
        ordered_row = namedtuple(
            "OrderedExecution",
            "task_id title recipient execution_id attempt",
        )(
            "task_1",
            "Build the app",
            "Code Engineer",
            "exec_1",
            1,
        )
        draft = {
            "checkpoint": {
                "activity": [
                    {
                        "id": "activity_1",
                        "name": "write_new_code",
                        "label": "Write code",
                    }
                ]
            }
        }
        ordered_result = MagicMock()
        ordered_result.all.return_value = [ordered_row]
        draft_result = MagicMock()
        draft_result.all.return_value = [("exec_1", draft)]
        db = MagicMock()
        db.execute.side_effect = [ordered_result, draft_result]

        activities = _activities_for_run(db, "run_1")

        assert [item.id for item in activities] == ["exec_1:activity_1"]
        assert activities[0].phase_label == "Build the app"
        assert db.execute.call_count == 2

        ordered_statement = db.execute.call_args_list[0].args[0]
        ordered_sql = str(
            ordered_statement.compile(
                dialect=mysql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()
        assert "order by" in ordered_sql
        assert "task_execution.draft" not in ordered_sql

        draft_statement = db.execute.call_args_list[1].args[0]
        draft_sql = str(
            draft_statement.compile(
                dialect=mysql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()
        assert "task_execution.draft" in draft_sql
        assert "order by" not in draft_sql
