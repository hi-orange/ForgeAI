import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


class _Inspector:
    def __init__(self) -> None:
        self.tables = {
            "build_run",
            "task_execution",
            "run_revision",
            "agent_decision_event",
            "agent_usage_event",
            "agent_eval_label",
        }
        self.indexes = {
            "run_revision": {"ix_run_revision_project_id"},
            "agent_decision_event": {
                "ix_agent_decision_event_project_id",
                "ix_agent_decision_event_build_run_id",
                "ix_agent_decision_event_message_id",
            },
            "agent_usage_event": {
                "ix_agent_usage_event_project_id",
                "ix_agent_usage_event_build_run_id",
                "ix_agent_usage_event_task_id",
            },
            "agent_eval_label": {"ix_agent_eval_label_project_id"},
        }
        self.checks = {
            "build_run": {
                "ck_build_run_status",
                "ck_build_run_stage_by_status",
                "ck_build_run_active_slot",
            },
            "task_execution": {"ck_task_execution_status"},
        }

    def has_table(self, table_name: str) -> bool:
        return table_name in self.tables

    def get_indexes(self, table_name: str) -> list[dict[str, str]]:
        return [{"name": name} for name in self.indexes.get(table_name, set())]

    def get_check_constraints(self, table_name: str) -> list[dict[str, str]]:
        return [{"name": name} for name in self.checks.get(table_name, set())]


class _Operations:
    def __init__(self, inspector: _Inspector) -> None:
        self.inspector = inspector
        self.created_tables: list[str] = []

    def get_bind(self) -> object:
        return self

    def create_table(self, table_name: str, *_elements: object) -> None:
        if table_name in self.inspector.tables:
            raise AssertionError(f"duplicate table creation: {table_name}")
        self.inspector.tables.add(table_name)
        self.created_tables.append(table_name)

    def create_index(self, name: str, table_name: str, _columns: list[str]) -> None:
        indexes = self.inspector.indexes.setdefault(table_name, set())
        if name in indexes:
            raise AssertionError(f"duplicate index creation: {name}")
        indexes.add(name)

    def drop_constraint(self, name: str, table_name: str, *, type_: str) -> None:
        self.inspector.checks[table_name].remove(name)

    def create_check_constraint(self, name: str, table_name: str, _condition: str) -> None:
        self.inspector.checks.setdefault(table_name, set()).add(name)


class RevisionMigrationRecoveryTests(unittest.TestCase):
    def test_upgrade_recovers_from_mysql_partial_ddl_and_can_replay(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "alembic/versions/a2c4e6f8b0d1_add_revision_events_deployments.py"
        )
        spec = importlib.util.spec_from_file_location("revision_migration", path)
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        inspector = _Inspector()
        operations = _Operations(inspector)
        with (
            patch.object(migration, "op", operations),
            patch.object(migration.sa, "inspect", return_value=inspector),
        ):
            migration.upgrade()
            migration.upgrade()

        self.assertEqual(operations.created_tables, ["deployment"])
        self.assertIn("ix_deployment_project_id", inspector.indexes["deployment"])


if __name__ == "__main__":
    unittest.main()
