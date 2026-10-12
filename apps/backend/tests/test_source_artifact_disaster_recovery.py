from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import ConflictException
from app.core.settings import settings
from app.db.database import Base
from app.generation.workspace import default_workspace_path
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem
from app.models.project import Project
from app.models.security_report import SecurityReport
from app.models.source_artifact import SourceArtifact
from app.models.user import User
from app.models.workflow_event import WorkflowEvent
from app.schemas.agent_action import ToolExecutionResult
from app.schemas.code_artifact import CODE_ARTIFACT_SCHEMA_VERSION, CodeArtifact
from app.services import security_gate
from app.services.engineering.completion import _source_manifest
from app.services.source_artifact import ensure_code_workspace, persist_source_artifact
from app.tools.checks import source_snapshot


class SourceArtifactDisasterRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = self.enterContext(TemporaryDirectory(prefix="forgeai-dr-"))
        root = Path(self.temp)
        self.runtime_root = root / "runtime"
        self.artifact_root = root / "artifacts"
        self.enterContext(patch.object(settings, "runtime_data_root", str(self.runtime_root)))
        self.enterContext(patch.object(settings, "source_artifact_root", str(self.artifact_root)))
        self.database_path = root / "recovery.db"
        self.engine = create_engine(
            f"sqlite+pysqlite:///{self.database_path}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions() as db:
            user = User(
                username="recovery-owner",
                email="recovery@example.com",
                hashed_password="unused",
            )
            db.add(user)
            db.flush()
            project = Project(
                user_id=user.id,
                name="Recoverable app",
                prompt="Build a recoverable app",
                status="draft",
            )
            db.add(project)
            db.flush()
            run = BuildRun(
                project_id=project.id,
                run_id="run_recovery",
                status="running",
                stage="runtime",
                active_slot=1,
            )
            db.add(run)
            db.commit()
            self.project_id = project.id
            self.run_id = run.run_id

        self.workspace = default_workspace_path(
            self.runtime_root,
            self.project_id,
            self.run_id,
        )
        (self.workspace / "backend" / "app").mkdir(parents=True)
        (self.workspace / "frontend" / "src").mkdir(parents=True)
        (self.workspace / "frontend" / "public" / "assets" / "images").mkdir(parents=True)
        (self.workspace / "backend" / "app" / "main.py").write_text(
            "from fastapi import FastAPI\napp = FastAPI()\n",
            encoding="utf-8",
        )
        (self.workspace / "frontend" / "package.json").write_text(
            '{"scripts":{"build":"vite build"}}\n',
            encoding="utf-8",
        )
        (self.workspace / "frontend" / "src" / "App.tsx").write_text(
            "export default function App(){return <main>Recovered</main>}\n",
            encoding="utf-8",
        )
        self.image_bytes = b"\x89PNG\r\n\x1a\nforgeai-recovery"
        (self.workspace / "frontend" / "public" / "assets" / "images" / "hero.png").write_bytes(
            self.image_bytes
        )
        self.snapshot, self.source_hash = source_snapshot(self.workspace)
        with self.sessions() as db:
            stored = persist_source_artifact(
                db,
                project_id=self.project_id,
                snapshot_bytes=self.snapshot,
                source_hash=self.source_hash,
            )
            code = CodeArtifact(
                source_hash=self.source_hash,
                source_artifact_id=stored.artifact_id,
                artifact_format=stored.artifact_format,
                artifact_size_bytes=stored.size_bytes,
                workspace_key=self.run_id,
                template_version="fullstack-react-v1",
                tool_strategy_version="test",
                files=_source_manifest(self.snapshot),
            )
            db.add(
                ConfigurationItem(
                    item_id="ci_recoverable_code",
                    project_id=self.project_id,
                    producer_run_id=self.run_id,
                    semantic_type="code",
                    version=1,
                    schema_version=CODE_ARTIFACT_SCHEMA_VERSION,
                    payload=code.model_dump(mode="json"),
                    content_hash="c" * 64,
                    upstream_item_ids=[],
                    state="usable",
                )
            )
            db.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_deleted_workspace_is_restored_after_database_reconnect(self) -> None:
        shutil.rmtree(self.workspace)
        self.engine.dispose()  # Simulate the worker/API process losing its pooled connections.

        with self.sessions() as db:
            restored_path, restored = ensure_code_workspace(
                db,
                project_id=self.project_id,
                run_id=self.run_id,
                code_item_id="ci_recoverable_code",
            )
            db.commit()

        self.assertTrue(restored)
        self.assertEqual(restored_path, self.workspace)
        self.assertEqual(source_snapshot(restored_path), (self.snapshot, self.source_hash))
        self.assertEqual(
            (restored_path / "frontend" / "public" / "assets" / "images" / "hero.png").read_bytes(),
            self.image_bytes,
        )
        with self.sessions() as db:
            event_row = db.scalar(
                select(WorkflowEvent).where(WorkflowEvent.event_type == "source_artifact_restored")
            )
            self.assertIsNotNone(event_row)
            assert event_row is not None
            self.assertEqual(event_row.payload["source_hash"], self.source_hash)

    def test_corrupt_durable_object_fails_closed_without_partial_workspace(self) -> None:
        shutil.rmtree(self.workspace)
        with self.sessions() as db:
            artifact = db.scalar(
                select(SourceArtifact).where(SourceArtifact.project_id == self.project_id)
            )
            assert artifact is not None
            object_path = self.artifact_root.joinpath(*artifact.object_key.split("/"))
            object_path.write_bytes(b"corrupt")

        with (
            self.sessions() as db,
            self.assertRaisesRegex(
                ConflictException,
                "完整性校验失败",
            ),
        ):
            ensure_code_workspace(
                db,
                project_id=self.project_id,
                run_id=self.run_id,
                code_item_id="ci_recoverable_code",
            )
        self.assertFalse(self.workspace.exists())

    def test_security_report_is_bound_to_exact_source_and_is_deployment_gate(self) -> None:
        output = (
            'FORGEAI_SECURITY_REPORT={"scanner_version":"forgeai-security-v1",'
            '"conclusion":"passed","findings":[]}'
        )
        result = ToolExecutionResult(
            tool_call_id="security:test",
            name="run_check",
            ok=True,
            summary="security 检查通过",
            data={"source_hash": self.source_hash, "output": output},
        )
        with self.sessions() as db, patch.object(security_gate, "run_check", return_value=result):
            report = security_gate.run_security_gate(
                db,
                project_id=self.project_id,
                run_id=self.run_id,
                code_item_id="ci_recoverable_code",
            )
            self.assertEqual(report.conclusion, "passed")
            required = security_gate.require_security_pass(
                db,
                project_id=self.project_id,
                run_id=self.run_id,
                code_item_id="ci_recoverable_code",
            )
            self.assertEqual(required.report_id, report.report_id)
            self.assertEqual(report.source_hash, self.source_hash)
            self.assertEqual(
                db.scalar(
                    select(SecurityReport).where(SecurityReport.report_id == report.report_id)
                ),
                report,
            )

    def test_missing_security_report_fails_closed(self) -> None:
        with self.sessions() as db, self.assertRaisesRegex(ConflictException, "安全扫描尚未完成"):
            security_gate.require_security_pass(
                db,
                project_id=self.project_id,
                run_id=self.run_id,
                code_item_id="ci_recoverable_code",
            )

    def test_existing_mismatched_workspace_is_never_overwritten(self) -> None:
        (self.workspace / "frontend" / "src" / "App.tsx").write_text(
            "export default function App(){return <main>Tampered</main>}\n",
            encoding="utf-8",
        )
        with (
            self.sessions() as db,
            self.assertRaisesRegex(
                ConflictException,
                "拒绝覆盖",
            ),
        ):
            ensure_code_workspace(
                db,
                project_id=self.project_id,
                run_id=self.run_id,
                code_item_id="ci_recoverable_code",
            )
        self.assertIn(
            "Tampered",
            (self.workspace / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8"),
        )

    def test_live_schema_v1_workspace_is_backfilled_without_rewriting_history(self) -> None:
        legacy_app = self.workspace / "frontend" / "src" / "App.tsx"
        legacy_app.write_text(
            "export default function App(){return <main>Legacy durable</main>}\n",
            encoding="utf-8",
        )
        legacy_snapshot, legacy_hash = source_snapshot(self.workspace)
        legacy_code = CodeArtifact(
            source_hash=legacy_hash,
            workspace_key=self.run_id,
            template_version="fullstack-react-v1",
            tool_strategy_version="legacy-test",
            files=_source_manifest(legacy_snapshot),
        )
        with self.sessions() as db:
            db.add(
                ConfigurationItem(
                    item_id="ci_legacy_code",
                    project_id=self.project_id,
                    producer_run_id=self.run_id,
                    semantic_type="code",
                    version=2,
                    schema_version=1,
                    payload=legacy_code.model_dump(mode="json", exclude_none=True),
                    content_hash="d" * 64,
                    upstream_item_ids=[],
                    state="usable",
                )
            )
            db.commit()
            _, backfilled = ensure_code_workspace(
                db,
                project_id=self.project_id,
                run_id=self.run_id,
                code_item_id="ci_legacy_code",
            )
            db.commit()
            self.assertTrue(backfilled)
            stored = db.scalar(
                select(SourceArtifact).where(SourceArtifact.source_hash == legacy_hash)
            )
            self.assertIsNotNone(stored)

        shutil.rmtree(self.workspace)
        with self.sessions() as db:
            _, restored = ensure_code_workspace(
                db,
                project_id=self.project_id,
                run_id=self.run_id,
                code_item_id="ci_legacy_code",
            )
            db.commit()
        self.assertTrue(restored)
        self.assertEqual(source_snapshot(self.workspace), (legacy_snapshot, legacy_hash))
