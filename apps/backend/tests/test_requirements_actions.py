"""Coarse requirements submit/start/continue actions."""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import func, select
from test_product_manager_workflow import ProductManagerWorkflowFixture

from app.core.exceptions import ConflictException
from app.models.plan import Plan
from app.models.project_message import ProjectMessage, ProjectMessageSender
from app.models.project_message_classification import ProjectMessageCategory
from app.schemas.project_message_classification import ProjectMessageClassificationDecision
from app.services import requirements


class RequirementsActionsTests(ProductManagerWorkflowFixture):
    def test_start_from_product_change_runs_workflow(self) -> None:
        with self.session_factory() as db:
            status = requirements.start_requirements(
                db, self.owner, self.project.id, self.message.id
            )
        self.assertEqual(status.state, "awaiting_approval")
        self.assertEqual(status.run_id, self.run.run_id)
        self.assertEqual(status.message_id, self.message.id)

    def test_start_inquiry_writes_assistant_guidance_without_planning(self) -> None:
        with self.session_factory() as db:
            inquiry = ProjectMessage(
                project_id=self.project.id,
                sequence=3,
                sender=ProjectMessageSender.USER.value,
                content="现在做到哪了？",
                client_message_id="inquiry-1",
            )
            db.add(inquiry)
            db.commit()
            db.refresh(inquiry)
            message_id = inquiry.id

        decision = ProjectMessageClassificationDecision(
            category=ProjectMessageCategory.INQUIRY,
            decision_summary="询问进度",
        )
        with (
            patch(
                "app.agents.manager.classify_message",
                return_value=decision,
            ),
            self.session_factory() as db,
        ):
            status = requirements.start_from_message(db, self.owner, self.project.id, message_id)
            guidance = db.scalar(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == self.project.id,
                    ProjectMessage.sender == ProjectMessageSender.ASSISTANT.value,
                )
            )

        self.assertEqual(status.state, "not_started")
        self.assertIsNotNone(guidance)
        assert guidance is not None
        self.assertIn("描述一下你想做的应用", guidance.content)
        self.assertEqual(guidance.client_message_id, f"guidance:msg:{message_id}:inquiry")

    def test_submit_on_not_started_creates_message_and_starts(self) -> None:
        with self.session_factory() as db:
            run = db.get(type(self.run), self.run.id)
            assert run is not None
            db.delete(run)
            for row in db.scalars(
                select(ProjectMessage).where(ProjectMessage.project_id == self.project.id)
            ).all():
                db.delete(row)
            db.commit()

        decision = ProjectMessageClassificationDecision(
            category=ProjectMessageCategory.PRODUCT_CHANGE,
            decision_summary="新产品需求",
        )
        with (
            patch(
                "app.agents.manager.classify_message",
                return_value=decision,
            ),
            self.session_factory() as db,
        ):
            status = requirements.submit_requirements(
                db,
                self.owner,
                self.project.id,
                "做一个记账应用",
                "submit-key-1",
            )

        self.assertEqual(status.state, "awaiting_approval")
        self.assertIsNotNone(status.run_id)
        with self.session_factory() as db:
            message = db.scalar(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == self.project.id,
                    ProjectMessage.client_message_id == "submit-key-1",
                )
            )
        self.assertIsNotNone(message)

    def test_submit_replay_after_state_advanced_does_not_become_clarification(self) -> None:
        with self.session_factory() as db:
            run = db.get(type(self.run), self.run.id)
            assert run is not None
            db.delete(run)
            for row in db.scalars(
                select(ProjectMessage).where(ProjectMessage.project_id == self.project.id)
            ).all():
                db.delete(row)
            db.commit()

        decision = ProjectMessageClassificationDecision(
            category=ProjectMessageCategory.PRODUCT_CHANGE,
            decision_summary="新产品需求",
        )
        with patch("app.agents.manager.classify_message", return_value=decision):
            with self.session_factory() as db:
                first = requirements.submit_requirements(
                    db, self.owner, self.project.id, "做一个记账应用", "submit-replay"
                )
            with self.session_factory() as db:
                replay = requirements.submit_requirements(
                    db, self.owner, self.project.id, "做一个记账应用", "submit-replay"
                )
                plan_count = db.scalar(
                    select(func.count()).select_from(Plan).where(Plan.project_id == self.project.id)
                )
                message_count = db.scalar(
                    select(func.count())
                    .select_from(ProjectMessage)
                    .where(ProjectMessage.project_id == self.project.id)
                )

        self.assertEqual(first.state, "awaiting_approval")
        self.assertEqual(replay.state, "awaiting_approval")
        self.assertEqual(replay.run_id, first.run_id)
        self.assertEqual(plan_count, 1)
        self.assertEqual(message_count, 1)
        self.assertEqual(self.chat.call_count, 1)

    def test_submit_replay_rejects_changed_content_even_after_state_advanced(self) -> None:
        with self.session_factory() as db:
            run = db.get(type(self.run), self.run.id)
            assert run is not None
            db.delete(run)
            for row in db.scalars(
                select(ProjectMessage).where(ProjectMessage.project_id == self.project.id)
            ).all():
                db.delete(row)
            db.commit()

        decision = ProjectMessageClassificationDecision(
            category=ProjectMessageCategory.PRODUCT_CHANGE,
            decision_summary="新产品需求",
        )
        with patch("app.agents.manager.classify_message", return_value=decision):
            with self.session_factory() as db:
                requirements.submit_requirements(
                    db, self.owner, self.project.id, "做一个记账应用", "submit-collision"
                )
            with self.session_factory() as db, self.assertRaises(ConflictException):
                requirements.submit_requirements(
                    db, self.owner, self.project.id, "改成商城", "submit-collision"
                )

    def test_http_start_without_payload_uses_first_message_and_continue_is_guarded(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.deps import get_current_user
        from app.api.v1.router import api_router
        from app.core.exceptions import register_exception_handlers
        from app.db.database import get_db

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(api_router)

        def override_db():
            with self.session_factory() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: self.owner
        client = TestClient(app)
        base = f"/api/v1/projects/{self.project.id}"

        started = client.post(f"{base}/requirements/start")
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["data"]["state"], "awaiting_approval")

        continued = client.post(f"{base}/requirements/continue")
        # awaiting_approval is not a continueable PM recovery state
        self.assertEqual(continued.status_code, 400)
