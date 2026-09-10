import importlib.util
import unittest
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    inspect,
    null,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.db.database import Base
from app.models.build_run import BuildRun, BuildRunStage, BuildRunStatus
from app.models.configuration_item import (
    ConfigurationItem,
    ConfigurationItemState,
    ConfigurationItemType,
)
from app.models.project import Project, ProjectStatus
from app.models.user import User
from app.schemas.configuration_item import ConfigurationItemOut, ConfigurationItemRegistration
from app.services import configuration_manager


class ConfigurationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

        with self.session_factory() as db:
            user = User(
                username="owner",
                email="owner@example.com",
                hashed_password="not-used",
            )
            db.add(user)
            db.flush()
            self.project = Project(
                user_id=user.id,
                name="Demo",
                prompt="Build a demo app",
                status=ProjectStatus.DRAFT.value,
            )
            self.other_project = Project(
                user_id=user.id,
                name="Other",
                prompt="Build another app",
                status=ProjectStatus.DRAFT.value,
            )
            db.add_all((self.project, self.other_project))
            db.flush()
            self.run = self._new_run(self.project.id, "run_current")
            self.other_run = self._new_run(self.other_project.id, "run_other")
            db.add_all((self.run, self.other_run))
            db.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _new_run(
        project_id: int,
        run_id: str,
        status: BuildRunStatus = BuildRunStatus.RUNNING,
    ) -> BuildRun:
        is_active = status in (BuildRunStatus.QUEUED, BuildRunStatus.RUNNING)
        return BuildRun(
            project_id=project_id,
            run_id=run_id,
            status=status.value,
            stage=BuildRunStage.PM.value if status == BuildRunStatus.RUNNING else None,
            active_slot=1 if is_active else null(),
        )

    @staticmethod
    def _submission(
        semantic_type: ConfigurationItemType,
        payload: dict,
        upstream_item_ids: list[str] | None = None,
    ) -> ConfigurationItemRegistration:
        return ConfigurationItemRegistration(
            semantic_type=semantic_type,
            payload=payload,
            upstream_item_ids=upstream_item_ids or [],
        )

    def _register(
        self,
        db,
        semantic_type: ConfigurationItemType,
        payload: dict,
        upstream_item_ids: list[str] | None = None,
        *,
        project_id: int | None = None,
        run_id: str | None = None,
    ) -> ConfigurationItem:
        return configuration_manager.register_configuration_item(
            db,
            project_id=project_id or self.project.id,
            producer_run_id=run_id or self.run.run_id,
            submission=self._submission(semantic_type, payload, upstream_item_ids),
        )

    def test_registers_versioned_chain_with_exact_upstream_ids(self) -> None:
        with self.session_factory() as db:
            spec_v1 = self._register(
                db,
                ConfigurationItemType.APP_SPEC,
                {"name": "Shop", "features": ["catalog"]},
            )
            spec_v2 = self._register(
                db,
                ConfigurationItemType.APP_SPEC,
                {"features": ["catalog"], "name": "Shop"},
                [spec_v1.item_id],
            )
            design = self._register(
                db,
                ConfigurationItemType.SYSTEM_DESIGN,
                {"backend": "FastAPI"},
                [spec_v2.item_id],
            )
            code = self._register(
                db,
                ConfigurationItemType.CODE,
                {"manifest": ["main.py"]},
                [design.item_id],
            )
            report = self._register(
                db,
                ConfigurationItemType.TEST_REPORT,
                {"passed": True},
                [code.item_id],
            )

        self.assertEqual(spec_v1.version, 1)
        self.assertEqual(spec_v2.version, 2)
        self.assertEqual(design.version, 1)
        self.assertEqual(code.version, 1)
        self.assertEqual(report.version, 1)
        self.assertEqual(spec_v2.upstream_item_ids, [spec_v1.item_id])
        self.assertEqual(design.upstream_item_ids, [spec_v2.item_id])
        self.assertEqual(code.upstream_item_ids, [design.item_id])
        self.assertEqual(report.upstream_item_ids, [code.item_id])
        self.assertEqual(spec_v1.content_hash, spec_v2.content_hash)
        self.assertEqual(len(report.content_hash), 64)
        self.assertEqual(report.state, ConfigurationItemState.USABLE.value)
        self.assertIsNone(report.unusable_reason)

        public_result = ConfigurationItemOut.model_validate(report)
        self.assertEqual(public_result.semantic_type, ConfigurationItemType.TEST_REPORT)
        self.assertEqual(public_result.upstream_item_ids, [code.item_id])

    def test_rejects_missing_wrong_unknown_and_cross_project_upstreams(self) -> None:
        with self.session_factory() as db:
            spec = self._register(
                db,
                ConfigurationItemType.APP_SPEC,
                {"name": "Demo"},
            )
            foreign_spec = self._register(
                db,
                ConfigurationItemType.APP_SPEC,
                {"name": "Other"},
                project_id=self.other_project.id,
                run_id=self.other_run.run_id,
            )

            with self.assertRaisesRegex(NotFoundException, "构建任务不存在"):
                self._register(
                    db,
                    ConfigurationItemType.APP_SPEC,
                    {"name": "Wrong run"},
                    run_id=self.other_run.run_id,
                )

            with self.assertRaisesRegex(BusinessException, "system_design 必须引用 app_spec"):
                self._register(db, ConfigurationItemType.SYSTEM_DESIGN, {"backend": "FastAPI"})

            design = self._register(
                db,
                ConfigurationItemType.SYSTEM_DESIGN,
                {"backend": "FastAPI"},
                [spec.item_id],
            )
            code = self._register(
                db,
                ConfigurationItemType.CODE,
                {"manifest": []},
                [design.item_id],
            )
            with self.assertRaisesRegex(
                BusinessException,
                "code 的直接上游只能是 app_spec 或 system_design",
            ):
                self._register(
                    db,
                    ConfigurationItemType.CODE,
                    {"manifest": ["other"]},
                    [code.item_id],
                )

            with self.assertRaisesRegex(BusinessException, "test_report 必须引用 code"):
                self._register(db, ConfigurationItemType.TEST_REPORT, {"passed": True})

            for invalid_id in ("ci_missing", foreign_spec.item_id):
                with (
                    self.subTest(item_id=invalid_id),
                    self.assertRaisesRegex(NotFoundException, "上游 ConfigurationItem"),
                ):
                    self._register(
                        db,
                        ConfigurationItemType.SYSTEM_DESIGN,
                        {"backend": "FastAPI"},
                        [invalid_id],
                    )

    def test_late_result_and_unusable_upstream_cannot_become_usable(self) -> None:
        with self.session_factory() as db:
            old_run = self._new_run(
                self.project.id,
                "run_failed",
                status=BuildRunStatus.FAILED,
            )
            db.add(old_run)
            db.commit()

            old_spec = self._register(
                db,
                ConfigurationItemType.APP_SPEC,
                {"name": "Old"},
                run_id=old_run.run_id,
            )
            derived_design = self._register(
                db,
                ConfigurationItemType.SYSTEM_DESIGN,
                {"backend": "FastAPI"},
                [old_spec.item_id],
            )

            self.assertEqual(old_spec.state, ConfigurationItemState.UNUSABLE.value)
            self.assertIn("producer_run_not_eligible:failed", old_spec.unusable_reason or "")
            self.assertEqual(derived_design.state, ConfigurationItemState.UNUSABLE.value)
            self.assertIn("upstream_unusable", derived_design.unusable_reason or "")

            for item in (old_spec, derived_design):
                with (
                    self.subTest(item_id=item.item_id),
                    self.assertRaisesRegex(ConflictException, "已过期"),
                ):
                    configuration_manager.require_usable_configuration_item(
                        db,
                        project_id=self.project.id,
                        item_id=item.item_id,
                    )

    def test_terminal_run_can_mark_existing_items_unusable_only_once(self) -> None:
        with self.session_factory() as db:
            item = self._register(
                db,
                ConfigurationItemType.APP_SPEC,
                {"name": "Current"},
            )

        with self.session_factory() as db:
            with self.assertRaisesRegex(ConflictException, "仍有提交资格"):
                configuration_manager.mark_run_configuration_items_unusable(
                    db,
                    project_id=self.project.id,
                    producer_run_id=self.run.run_id,
                    reason="requirements changed",
                )

        with self.session_factory() as db:
            run = db.scalar(select(BuildRun).where(BuildRun.run_id == self.run.run_id))
            self.assertIsNotNone(run)
            run.status = BuildRunStatus.FAILED.value
            run.active_slot = None
            db.commit()

        with self.session_factory() as db:
            changed = configuration_manager.mark_run_configuration_items_unusable(
                db,
                project_id=self.project.id,
                producer_run_id=self.run.run_id,
                reason="requirements changed",
            )
            replay = configuration_manager.mark_run_configuration_items_unusable(
                db,
                project_id=self.project.id,
                producer_run_id=self.run.run_id,
                reason="requirements changed",
            )
            stored = configuration_manager.get_configuration_item(
                db,
                project_id=self.project.id,
                item_id=item.item_id,
            )

        self.assertEqual(changed, 1)
        self.assertEqual(replay, 0)
        self.assertEqual(stored.state, ConfigurationItemState.UNUSABLE.value)
        self.assertEqual(stored.unusable_reason, "requirements changed")
        self.assertIsNotNone(stored.unusable_at)
        self.assertEqual(stored.payload, {"name": "Current"})

        with self.session_factory() as db:
            unusable_item = db.get(ConfigurationItem, item.id)
            self.assertIsNotNone(unusable_item)
            unusable_item.state = ConfigurationItemState.USABLE.value
            unusable_item.unusable_reason = None
            unusable_item.unusable_at = None
            with self.assertRaisesRegex(ValueError, "单向变为 unusable"):
                db.commit()
            db.rollback()

    def test_registered_content_cannot_be_overwritten(self) -> None:
        with self.session_factory() as db:
            item = self._register(
                db,
                ConfigurationItemType.APP_SPEC,
                {"name": "Original"},
            )

        with self.session_factory() as db:
            stored = db.get(ConfigurationItem, item.id)
            self.assertIsNotNone(stored)
            stored.payload = {"name": "Overwritten"}
            with self.assertRaisesRegex(ValueError, "不可修改"):
                db.commit()
            db.rollback()

        with self.session_factory() as db:
            unchanged = db.get(ConfigurationItem, item.id)
            self.assertIsNotNone(unchanged)
            self.assertEqual(unchanged.payload, {"name": "Original"})

    def test_database_enforces_versions_types_and_unusable_fields(self) -> None:
        with self.session_factory() as db:
            valid = self._register(
                db,
                ConfigurationItemType.APP_SPEC,
                {"name": "Valid"},
            )

            invalid_rows = (
                ConfigurationItem(
                    item_id="ci_invalid_type",
                    project_id=self.project.id,
                    producer_run_id=self.run.run_id,
                    semantic_type="unknown",
                    version=2,
                    schema_version=1,
                    payload={"name": "Invalid"},
                    content_hash="a" * 64,
                    upstream_item_ids=[],
                ),
                ConfigurationItem(
                    item_id="ci_duplicate_version",
                    project_id=self.project.id,
                    producer_run_id=self.run.run_id,
                    semantic_type=ConfigurationItemType.APP_SPEC.value,
                    version=valid.version,
                    schema_version=1,
                    payload={"name": "Duplicate"},
                    content_hash="b" * 64,
                    upstream_item_ids=[],
                ),
                ConfigurationItem(
                    item_id="ci_invalid_unusable",
                    project_id=self.project.id,
                    producer_run_id=self.run.run_id,
                    semantic_type=ConfigurationItemType.APP_SPEC.value,
                    version=2,
                    schema_version=1,
                    payload={"name": "Invalid unusable"},
                    content_hash="c" * 64,
                    upstream_item_ids=[],
                    state=ConfigurationItemState.UNUSABLE.value,
                ),
            )

            for row in invalid_rows:
                with self.subTest(item_id=row.item_id):
                    db.add(row)
                    with self.assertRaises(IntegrityError):
                        db.commit()
                    db.rollback()

    def test_registration_schema_rejects_duplicate_ids_and_non_json_content(self) -> None:
        with self.assertRaises(ValidationError):
            ConfigurationItemRegistration(
                semantic_type=ConfigurationItemType.SYSTEM_DESIGN,
                payload={"backend": "FastAPI"},
                upstream_item_ids=["ci_same", "ci_same"],
            )
        with self.assertRaises(ValidationError):
            ConfigurationItemRegistration(
                semantic_type=ConfigurationItemType.APP_SPEC,
                payload={"invalid": object()},
            )

    def test_project_delete_cascades_to_configuration_items(self) -> None:
        with self.session_factory() as db:
            item = self._register(
                db,
                ConfigurationItemType.APP_SPEC,
                {"name": "Temporary"},
            )
            project = db.get(Project, self.project.id)
            self.assertIsNotNone(project)
            db.delete(project)
            db.commit()

        with self.session_factory() as db:
            self.assertIsNone(db.get(ConfigurationItem, item.id))
            self.assertIsNone(db.scalar(select(BuildRun).where(BuildRun.run_id == self.run.run_id)))

    def test_migration_upgrade_and_downgrade(self) -> None:
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "7c1f3e9a4d2b_create_configuration_item.py"
        )
        spec = importlib.util.spec_from_file_location(
            "configuration_item_migration",
            migration_path,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        legacy_engine = create_engine("sqlite+pysqlite://")
        metadata = MetaData()
        Table("project", metadata, Column("id", Integer, primary_key=True))
        Table(
            "build_run",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("project_id", Integer, nullable=False),
            Column("run_id", String(length=40), nullable=False, unique=True),
        )
        metadata.create_all(legacy_engine)

        with legacy_engine.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            self.assertIn("configuration_item", inspect(connection).get_table_names())
            column_names = {
                column["name"] for column in inspect(connection).get_columns("configuration_item")
            }
            self.assertIn("upstream_item_ids", column_names)
            self.assertIn("content_hash", column_names)
            migration.downgrade()
            self.assertNotIn("configuration_item", inspect(connection).get_table_names())

        legacy_engine.dispose()


if __name__ == "__main__":
    unittest.main()
