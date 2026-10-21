"""Workspace file listing/read after requirements approval."""

from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import ConflictException, NotFoundException
from app.core.settings import settings
from app.db.database import Base
from app.generation.workspace import prepare_engineering_workspace
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.project import Project
from app.models.project_message import ProjectMessage
from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.models.task_result import TaskResult
from app.models.user import User
from app.schemas.requirements import RequirementsStatus
from app.services import workspace_files as workspace_files_service
from app.services.source_artifact import persist_source_artifact
from app.services.task_execution import utc_now
from app.services.workflow_events import append_event
from app.services.workspace_documents import run_documents
from app.tools.checks import source_snapshot


class WorkspaceFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="forgeai-ws-files-"))
        self.workspace_root = self.enterContext(tempfile.TemporaryDirectory(prefix="forgeai-ws-"))
        self.artifact_root = self.enterContext(
            tempfile.TemporaryDirectory(prefix="forgeai-source-artifacts-")
        )
        self.enterContext(patch.object(settings, "runtime_data_root", self.workspace_root))
        self.enterContext(patch.object(settings, "source_artifact_root", self.artifact_root))
        self.engine = create_engine(
            f"sqlite+pysqlite:///{Path(directory) / 'ws.db'}",
            connect_args={"check_same_thread": False},
        )
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.session_factory() as db:
            user = User(username="ws-owner", email="ws@test.com", hashed_password="x")
            db.add(user)
            db.flush()
            project = Project(user_id=user.id, name="WS", prompt="做招聘站", status="draft")
            db.add(project)
            db.flush()
            run = BuildRun(
                project_id=project.id,
                run_id="run_workspace_files",
                status="running",
                stage="pm",
                active_slot=1,
            )
            db.add(run)
            db.commit()
            self.user_id = user.id
            self.project_id = project.id
            self.run_id = run.run_id

    def _owner(self) -> User:
        with self.session_factory() as db:
            return db.get(User, self.user_id)

    def _document_fixture(self, db, *, design=True):
        def artifact(item_id, kind, version, payload, upstream):
            item = ConfigurationItem(
                item_id=item_id,
                project_id=self.project_id,
                producer_run_id=self.run_id,
                semantic_type=kind,
                version=version,
                schema_version=1,
                payload=payload,
                content_hash="a" * 64,
                upstream_item_ids=upstream,
                state="usable",
            )
            db.add(item)
            return item

        artifact(
            "ci_spec",
            "app_spec",
            1,
            {"goal": "招聘应用", "features": [{"id": "jobs", "text": "浏览职位"}]},
            [],
        )
        if design:
            artifact(
                "ci_design",
                "system_design",
                1,
                {
                    "modules": [{"name": "职位服务", "responsibility": "查询职位"}],
                    "interfaces": [{"name": "职位列表", "method": "GET", "path": "/api/jobs"}],
                    "architecture_overview": "冻结设计一",
                },
                ["ci_spec"],
            )
            artifact(
                "ci_unreferenced",
                "system_design",
                2,
                {"architecture_overview": "未引用的新设计"},
                ["ci_spec"],
            )
        message = ProjectMessage(
            project_id=self.project_id,
            sequence=1,
            sender="user",
            content="构建",
            client_message_id="docs",
        )
        db.add(message)
        db.flush()
        db.add(
            Plan(
                plan_id="plan_docs",
                project_id=self.project_id,
                build_run_id=self.run_id,
                version=1,
                cause_message_id=message.id,
                definition_hash="b" * 64,
                status="running",
            )
        )
        db.flush()
        db.add(
            Task(
                task_id="task_docs",
                plan_id="plan_docs",
                task_key="engineering_delivery",
                position=1,
                recipient="Code Engineer",
                title="实现招聘应用",
                instructions="实现",
                expected_output_type="code",
                input_configuration_item_ids=["ci_design" if design else "ci_spec"],
                depends_on_task_ids=[],
                status="running",
            )
        )
        db.commit()
        return RequirementsStatus(
            project_id=self.project_id,
            run_id=self.run_id,
            task_id="task_docs",
            state="engineering_running",
        )

    def test_saved_documents_visible_before_any_code_revision(self):
        with self.session_factory() as db:
            status = self._document_fixture(db)
            with patch("app.services.workspace_files.get_requirements_status", return_value=status):
                listing = workspace_files_service.list_project_workspace(
                    db, self._owner(), self.project_id, self.run_id
                )
                self.assertTrue(listing.ready)
                self.assertEqual(
                    {f.path for f in listing.files},
                    {"forgeai/ARCHITECTURE.md", "forgeai/REQUIREMENTS.md", "forgeai/PROGRESS.md"},
                )
                self.assertTrue(all(f.kind == "document" for f in listing.files))
                document = workspace_files_service.read_project_workspace_file(
                    db, self._owner(), self.project_id, "forgeai/ARCHITECTURE.md", self.run_id
                )
                self.assertEqual(document.kind, "document")
                self.assertEqual(document.run_id, self.run_id)
                self.assertIn("冻结设计一", document.content)
                self.assertIn("版本：v1", document.content)
                self.assertIn("ci_design", document.content)
                self.assertIn("GET", document.content)
                self.assertLess(
                    document.content.index("## 架构概览"), document.content.index("## 模块划分")
                )
                self.assertLess(
                    document.content.index("## 接口契约"), document.content.index("## 版本来源")
                )
                self.assertNotIn("未引用的新设计", document.content)
                with self.assertRaisesRegex(ConflictException, "revision 尚未生成"):
                    workspace_files_service.read_project_workspace_file(
                        db, self._owner(), self.project_id, "frontend/src/App.tsx"
                    )
            self.assertFalse((Path(self.workspace_root) / "work").exists())

    def test_missing_design_is_not_fabricated_and_documents_survive_stop(self):
        with self.session_factory() as db:
            status = self._document_fixture(db, design=False)
            status.state = "stopped"
            with patch("app.services.workspace_files.get_requirements_status", return_value=status):
                listing = workspace_files_service.list_project_workspace(
                    db, self._owner(), self.project_id
                )
                self.assertEqual(
                    [f.path for f in listing.files],
                    ["forgeai/PROGRESS.md", "forgeai/REQUIREMENTS.md"],
                )
                with self.assertRaises(NotFoundException):
                    workspace_files_service.read_project_workspace_file(
                        db, self._owner(), self.project_id, "forgeai/ARCHITECTURE.md"
                    )

    def _saved_engineering_execution(self, db, *, accepted=True):
        execution = TaskExecution(
            execution_id="exec_docs",
            task_id="task_docs",
            attempt=1,
            status="running",
            active_slot=1,
            started_at=utc_now(),
            expires_at=utc_now() + timedelta(minutes=10),
            draft={
                "kind": "engineering_input_snapshot",
                "schema_version": 4,
                "project_id": self.project_id,
                "run_id": self.run_id,
                "task_id": "task_docs",
                "design_item_id": "ci_design",
                "approved_item_id": "ci_spec",
                "checkpoint": {
                    "kind": "engineering_checkpoint",
                    "delivery_plan_version": 1,
                    "needs_delivery_plan": not accepted,
                    "current_work_item_id": "jobs_ui",
                    "work_items": [
                        {
                            "id": "jobs_api",
                            "title": "职位接口",
                            "status": "completed",
                            "requirement_ids": ["jobs"],
                        },
                        {
                            "id": "jobs_ui",
                            "title": "职位页面",
                            "status": "pending",
                            "requirement_ids": ["jobs"],
                            "depends_on": ["jobs_api"],
                            "deliverables": ["frontend/src/Jobs.tsx"],
                            "acceptance": ["可浏览职位"],
                        },
                    ],
                },
            },
        )
        db.add(execution)
        db.commit()
        return execution

    def test_progress_uses_accepted_work_items_and_survives_pause_without_claiming_acceptance(self):
        with self.session_factory() as db:
            status = self._document_fixture(db)
            execution = self._saved_engineering_execution(db)
            before = str(execution.draft)
            documents = run_documents(db, self.project_id, status)
            progress = documents["forgeai/PROGRESS.md"].decode()
            self.assertIn("状态：工程检查已通过", progress)
            self.assertIn("状态：当前工作项", progress)
            self.assertIn("前置工作项：\n\n- jobs_api", progress)
            self.assertIn("frontend/src/Jobs.tsx", progress)
            self.assertIn("exec_docs", progress)
            self.assertNotIn("独立验证已通过", progress)
            self.assertIn("## 实施指引", documents["forgeai/ARCHITECTURE.md"].decode())
            status.state = "retry_available"
            status.error = "检查发现接口返回错误"
            paused = run_documents(db, self.project_id, status)["forgeai/PROGRESS.md"].decode()
            self.assertIn("进度已保存，当前未执行", paused)
            self.assertIn("当前问题：检查发现接口返回错误", paused)
            self.assertIn("状态：尚未完成", paused)
            self.assertNotIn("状态：当前工作项", paused)
            self.assertEqual(str(execution.draft), before)
            self.assertFalse(db.dirty)

    def test_unaccepted_or_wrong_run_checkpoint_is_not_presented_as_an_implementation_plan(self):
        with self.session_factory() as db:
            status = self._document_fixture(db)
            execution = self._saved_engineering_execution(db, accepted=False)
            progress = run_documents(db, self.project_id, status)["forgeai/PROGRESS.md"].decode()
            self.assertIn("尚无本轮已接受", progress)
            self.assertNotIn("### 职位接口", progress)
            execution.draft = {**execution.draft, "run_id": "run_other"}
            db.commit()
            progress = run_documents(db, self.project_id, status)["forgeai/PROGRESS.md"].decode()
            self.assertNotIn("exec_docs", progress)
            self.assertNotIn("### 职位页面", progress)

    def test_progress_uses_status_bound_attempt_and_plan_during_recovery(self):
        with self.session_factory() as db:
            status = self._document_fixture(db)
            previous = self._saved_engineering_execution(db)
            previous.status, previous.active_slot = "failed", None
            db.flush()
            recovered = TaskExecution(
                execution_id="exec_docs_recovered",
                task_id="task_docs",
                attempt=2,
                status="running",
                active_slot=1,
                started_at=utc_now(),
                expires_at=utc_now() + timedelta(minutes=10),
                draft={
                    **previous.draft,
                    "checkpoint": {
                        **previous.draft["checkpoint"],
                        "work_items": [
                            {"id": "repaired_ui", "title": "修复职位页面", "status": "pending"}
                        ],
                        "current_work_item_id": "repaired_ui",
                    },
                },
            )
            db.add(recovered)
            original = db.query(Plan).filter_by(plan_id="plan_docs").one()
            db.add(
                Plan(
                    plan_id="plan_newer_unbound",
                    project_id=self.project_id,
                    build_run_id=self.run_id,
                    version=2,
                    cause_message_id=original.cause_message_id,
                    definition_hash="f" * 64,
                    status="pending",
                )
            )
            db.commit()
            status.plan_id = "plan_docs"
            status.execution_id = "exec_docs"
            old = run_documents(db, self.project_id, status)["forgeai/PROGRESS.md"].decode()
            self.assertIn("### 职位页面", old)
            self.assertNotIn("修复职位页面", old)
            self.assertNotIn("plan_newer_unbound", old)
            status.execution_id = "exec_docs_recovered"
            current = run_documents(db, self.project_id, status)["forgeai/PROGRESS.md"].decode()
            self.assertIn("### 修复职位页面", current)
            self.assertIn("exec_docs_recovered", current)
            self.assertNotIn("### 职位接口", current)

    def test_quality_progress_keeps_exact_producing_engineering_plan(self):
        with self.session_factory() as db:
            status = self._document_fixture(db)
            execution = self._saved_engineering_execution(db)
            execution.status, execution.active_slot = "succeeded", None
            db.add(
                ConfigurationItem(
                    item_id="ci_code_progress",
                    project_id=self.project_id,
                    producer_run_id=self.run_id,
                    semantic_type="code",
                    version=1,
                    schema_version=1,
                    payload={},
                    content_hash="d" * 64,
                    upstream_item_ids=["ci_design"],
                    state="usable",
                )
            )
            db.flush()
            db.add(
                TaskResult(
                    task_id="task_docs",
                    configuration_item_id="ci_code_progress",
                    result_hash="e" * 64,
                    source_message_ids=[],
                    context_truncated=False,
                    model="fixture",
                    prompt_version="fixture",
                )
            )
            original = db.query(Plan).filter_by(plan_id="plan_docs").one()
            db.add(
                Plan(
                    plan_id="plan_quality_docs",
                    project_id=self.project_id,
                    build_run_id=self.run_id,
                    version=2,
                    cause_message_id=original.cause_message_id,
                    definition_hash="f" * 64,
                    status="running",
                )
            )
            db.flush()
            db.add(
                Task(
                    task_id="task_quality_docs",
                    plan_id="plan_quality_docs",
                    task_key="quality",
                    position=1,
                    recipient="Test Engineer",
                    title="独立验证",
                    instructions="验证",
                    expected_output_type="test_report",
                    input_configuration_item_ids=["ci_code_progress"],
                    depends_on_task_ids=[],
                    status="running",
                )
            )
            db.commit()
            status.code_item_id = "ci_code_progress"
            status.task_id = "task_quality_docs"
            status.state = "quality_running"
            progress = run_documents(db, self.project_id, status)["forgeai/PROGRESS.md"].decode()
            self.assertIn("当前状态：正在独立验证", progress)
            self.assertIn("### 职位接口", progress)
            self.assertIn("工程执行：exec_docs", progress)
            self.assertIn("阶段计划：plan_quality_docs", progress)
            self.assertNotIn("独立验证已通过", progress)

    def test_document_reads_enforce_owner_path_and_requested_run(self):
        with self.session_factory() as db:
            status = self._document_fixture(db)
            stranger = User(
                id=999, username="stranger", email="stranger@test.com", hashed_password="x"
            )
            with patch("app.services.workspace_files.get_requirements_status", return_value=status):
                with self.assertRaises(NotFoundException):
                    workspace_files_service.list_project_workspace(db, stranger, self.project_id)
                with self.assertRaises(NotFoundException):
                    workspace_files_service.read_project_workspace_file(
                        db, stranger, self.project_id, "forgeai/ARCHITECTURE.md"
                    )
                for path in ["../forgeai/ARCHITECTURE.md", "/forgeai/ARCHITECTURE.md"]:
                    with self.assertRaises(ConflictException):
                        workspace_files_service.read_project_workspace_file(
                            db, self._owner(), self.project_id, path
                        )
                with self.assertRaisesRegex(ConflictException, "构建已切换"):
                    workspace_files_service.list_project_workspace(
                        db, self._owner(), self.project_id, "run_other"
                    )
                with self.assertRaisesRegex(ConflictException, "构建已切换"):
                    workspace_files_service.read_project_workspace_file(
                        db, self._owner(), self.project_id, "forgeai/ARCHITECTURE.md", "run_other"
                    )

    def test_published_code_uses_its_exact_upstream_design(self):
        with self.session_factory() as db:
            status = self._document_fixture(db)
            db.add(
                ConfigurationItem(
                    item_id="ci_code",
                    project_id=self.project_id,
                    producer_run_id=self.run_id,
                    semantic_type="code",
                    version=1,
                    schema_version=1,
                    payload={},
                    content_hash="c" * 64,
                    upstream_item_ids=["ci_unreferenced"],
                    state="usable",
                )
            )
            db.commit()
            status.code_item_id = "ci_code"
            documents = run_documents(db, self.project_id, status)
            self.assertIn("未引用的新设计", documents["forgeai/ARCHITECTURE.md"].decode())
            self.assertNotIn("冻结设计一", documents["forgeai/ARCHITECTURE.md"].decode())

    def _publish_revision(self, db, revision: int = 1) -> str:
        root = Path(settings.runtime_data_root) / "work" / str(self.project_id) / self.run_id
        snapshot, source_hash = source_snapshot(root)
        artifact = persist_source_artifact(
            db,
            project_id=self.project_id,
            snapshot_bytes=snapshot,
            source_hash=source_hash,
        )
        event = append_event(
            db,
            idempotency_key=f"test-preview:{self.run_id}:{revision}",
            stream_key=f"preview:{self.run_id}",
            event_type="preview_revision_published",
            project_id=self.project_id,
            build_run_id=self.run_id,
            payload={
                "source_hash": source_hash,
                "source_artifact_id": artifact.artifact_id,
            },
        )
        self.assertEqual(event.sequence, revision)
        db.commit()
        return source_hash

    def test_list_and_read_after_prepare(self) -> None:
        prepare_engineering_workspace(
            self.project_id,
            self.run_id,
            task_id="task_ws",
            approved_item_id="ci_ws",
            app_spec={"goal": "招聘站"},
        )
        with self.session_factory() as db:
            source_hash = self._publish_revision(db)
            root = Path(settings.runtime_data_root) / "work" / str(self.project_id) / self.run_id
            (root / "frontend/src/App.tsx").write_text("UNPUBLISHED", encoding="utf-8")
            with patch("app.services.workspace_files.get_requirements_status") as status:
                from app.schemas.requirements import RequirementsStatus

                status.return_value = RequirementsStatus(
                    project_id=self.project_id,
                    run_id=self.run_id,
                    state="engineering_generated",
                    workspace_ready=True,
                    code_ready=True,
                    preview_revision=1,
                    preview_source_hash=source_hash,
                )
                listing = workspace_files_service.list_project_workspace(
                    db, self._owner(), self.project_id
                )
                self.assertTrue(listing.ready)
                paths = {item.path for item in listing.files}
                self.assertIn("frontend/src/App.tsx", paths)
                self.assertIn("backend/app/main.py", paths)
                self.assertTrue(all(not path.startswith("forgeai/") for path in paths))
                app = workspace_files_service.read_project_workspace_file(
                    db, self._owner(), self.project_id, "frontend/src/App.tsx"
                )
                self.assertNotIn("UNPUBLISHED", app.content)
                content = workspace_files_service.read_project_workspace_file(
                    db, self._owner(), self.project_id, "backend/app/api/health.py"
                )
                self.assertEqual(content.kind, "text")
                self.assertIn("health", content.content)
                with self.assertRaises(ConflictException):
                    workspace_files_service.read_project_workspace_file(
                        db, self._owner(), self.project_id, "../secret.txt"
                    )

    def test_list_and_preview_generated_images(self) -> None:
        prepare_engineering_workspace(
            self.project_id,
            self.run_id,
            task_id="task_ws_image",
            approved_item_id="ci_ws_image",
            app_spec={"goal": "咖啡馆"},
        )
        root = Path(settings.runtime_data_root) / "work" / str(self.project_id) / self.run_id
        image = root / "frontend/public/images/cafe-hero.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"\xff\xd8\xfffake-jpeg-bytes")
        with self.session_factory() as db:
            source_hash = self._publish_revision(db)
            with patch("app.services.workspace_files.get_requirements_status") as status:
                from app.schemas.requirements import RequirementsStatus

                status.return_value = RequirementsStatus(
                    project_id=self.project_id,
                    run_id=self.run_id,
                    state="engineering_generated",
                    workspace_ready=True,
                    code_ready=True,
                    preview_revision=1,
                    preview_source_hash=source_hash,
                )
                listing = workspace_files_service.list_project_workspace(
                    db, self._owner(), self.project_id
                )
                image_entry = next(
                    item
                    for item in listing.files
                    if item.path == "frontend/public/images/cafe-hero.jpg"
                )
                self.assertEqual(image_entry.kind, "image")
                preview = workspace_files_service.read_project_workspace_file(
                    db,
                    self._owner(),
                    self.project_id,
                    "frontend/public/images/cafe-hero.jpg",
                )
                self.assertEqual(preview.kind, "image")
                self.assertEqual(preview.media_type, "image/jpeg")
                self.assertEqual(
                    preview.content,
                    base64.b64encode(b"\xff\xd8\xfffake-jpeg-bytes").decode("ascii"),
                )

    def test_template_stays_hidden_until_a_source_revision_exists(self) -> None:
        prepare_engineering_workspace(
            self.project_id,
            self.run_id,
            task_id="task_ws",
            approved_item_id="ci_ws",
            app_spec={"goal": "招聘站"},
        )
        with self.session_factory() as db:
            with patch("app.services.workspace_files.get_requirements_status") as status:
                from app.schemas.requirements import RequirementsStatus

                status.return_value = RequirementsStatus(
                    project_id=self.project_id,
                    run_id=self.run_id,
                    state="engineering_running",
                    workspace_ready=True,
                    code_ready=False,
                )
                listing = workspace_files_service.list_project_workspace(
                    db, self._owner(), self.project_id
                )
                self.assertFalse(listing.ready)
                self.assertEqual(listing.files, [])
                with self.assertRaisesRegex(ConflictException, "revision 尚未生成"):
                    workspace_files_service.read_project_workspace_file(
                        db, self._owner(), self.project_id, "frontend/src/App.tsx"
                    )


class WorkspaceReadyOnPauseTests(unittest.TestCase):
    def test_retry_and_stopped_still_expose_workspace(self) -> None:
        from unittest.mock import MagicMock

        from app.schemas.requirements import RequirementsStatus
        from app.services.requirements import _attach_workspace_status

        for state in ("retry_available", "stopped"):
            status = RequirementsStatus(project_id=9, run_id="run_pause", state=state)
            with (
                patch("app.services.requirements.workspace_is_ready", return_value=True),
                patch.object(settings, "runtime_data_root", "runtime-data"),
            ):
                out = _attach_workspace_status(MagicMock(), status)
            self.assertTrue(out.workspace_ready, state)
            self.assertTrue(out.workspace_path)


if __name__ == "__main__":
    unittest.main()
