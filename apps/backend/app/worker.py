"""Durable database worker for long-running ForgeAI role and deployment jobs."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from uuid import uuid4

from sqlalchemy import select

from app.core.exceptions import AppException, ConflictException, WorkflowBlockedException
from app.core.settings import settings
from app.db.database import SessionLocal
from app.models.configuration_item import ConfigurationItem
from app.models.task_execution import TaskExecution
from app.models.task_result import TaskResult
from app.models.user import User
from app.models.workflow_job import WorkflowJob
from app.services.task_execution import latest_execution
from app.services.workflow_jobs import (
    attach_execution,
    claim_next_job,
    complete_job,
    fail_job,
    heartbeat_job,
    recover_expired_jobs,
    recover_orphaned_work,
    recover_unapplied_decisions,
)

logger = logging.getLogger("forgeai.worker")


def _required_payload(job: WorkflowJob, key: str) -> str:
    value = job.payload.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"workflow job 缺少 {key}")
    return str(value)


def _owner(job: WorkflowJob) -> User:
    user_id = int(_required_payload(job, "user_id"))
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None:
            raise ValueError("workflow job 用户不存在")
        db.expunge(user)
        return user


def _existing_result(db, task_id: str) -> ConfigurationItem | None:
    saved = db.get(TaskResult, task_id)
    if saved is None:
        return None
    return db.scalar(
        select(ConfigurationItem).where(ConfigurationItem.item_id == saved.configuration_item_id)
    )


def _latest_recovery_id(db, task_id: str) -> str | None:
    current = latest_execution(db, task_id)
    if current is None or current.status == "succeeded":
        return None
    return current.execution_id


def _handle_product_manager(job: WorkflowJob, worker_id: str) -> None:
    from app.orchestration.product_manager import run_product_manager_workflow

    user = _owner(job)
    project_id = int(_required_payload(job, "project_id"))
    run_id = _required_payload(job, "run_id")
    task_id = _required_payload(job, "task_id")
    message_id = int(_required_payload(job, "message_id"))
    with SessionLocal() as db:
        if _existing_result(db, task_id) is not None:
            return
        recovery_id = str(job.payload.get("recovery_execution_id") or "") or (
            _latest_recovery_id(db, task_id)
        )
        run_product_manager_workflow(
            db,
            user,
            project_id,
            run_id,
            message_id,
            recovery_execution_id=recovery_id,
            on_execution_started=lambda execution: attach_execution(
                db, job.job_id, worker_id, execution
            ),
        )


def _handle_architect(job: WorkflowJob, worker_id: str) -> None:
    from app.orchestration.leader import run_architect_and_continue, start_dispatched_delivery
    from app.services import leader as leader_service

    user = _owner(job)
    project_id = int(_required_payload(job, "project_id"))
    run_id = _required_payload(job, "run_id")
    task_id = _required_payload(job, "task_id")
    with SessionLocal() as db:
        existing = _existing_result(db, task_id)
        if existing is not None:
            delivery = leader_service.dispatch_completed_design(
                db, user, project_id, run_id, existing.item_id
            )
            start_dispatched_delivery(db, user, project_id, run_id, delivery)
            return
        recovery_id = str(job.payload.get("recovery_execution_id") or "") or _latest_recovery_id(
            db, task_id
        )
        run_architect_and_continue(
            db,
            user,
            project_id,
            run_id,
            task_id,
            recovery_execution_id=recovery_id,
            on_execution_started=lambda execution: attach_execution(
                db, job.job_id, worker_id, execution
            ),
        )


def _handle_engineering(job: WorkflowJob, worker_id: str) -> None:
    from app.services.engineering import claim_code_engineer_task
    from app.services.engineering.run import _run_and_publish, dispatch_code_validation

    user = _owner(job)
    project_id = int(_required_payload(job, "project_id"))
    run_id = _required_payload(job, "run_id")
    task_id = _required_payload(job, "task_id")
    with SessionLocal() as db:
        existing = _existing_result(db, task_id)
        if existing is not None:
            dispatch_code_validation(db, user, project_id, run_id, existing.item_id)
            return
        execution = db.get(TaskExecution, job.execution_id) if job.execution_id else None
        if execution is None or execution.status != "running":
            recovery_id = str(job.payload.get("recovery_execution_id") or "") or (
                execution.execution_id
                if execution is not None
                else _latest_recovery_id(db, task_id)
            )
            _, execution = claim_code_engineer_task(
                db,
                user,
                project_id,
                run_id,
                task_id,
                recovery_execution_id=recovery_id,
            )
            attach_execution(db, job.job_id, worker_id, execution)
        result = _run_and_publish(
            db,
            user,
            project_id,
            run_id,
            task_id,
            execution.execution_id,
            enqueue_quality=True,
        )
        outcome = str(result.get("outcome") or "unknown")
        if outcome != "generated":
            raise WorkflowBlockedException(f"Code Engineer 已暂停（{outcome}），等待用户显式继续")


def _handle_quality(job: WorkflowJob, worker_id: str) -> None:
    from app.orchestration.test_engineer import (
        route_completed_test_report,
        run_test_engineer_task,
    )

    user = _owner(job)
    project_id = int(_required_payload(job, "project_id"))
    run_id = _required_payload(job, "run_id")
    task_id = _required_payload(job, "task_id")
    with SessionLocal() as db:
        existing = _existing_result(db, task_id)
        if existing is not None:
            route_completed_test_report(db, user, project_id, run_id, existing)
            return
        recovery_id = str(job.payload.get("recovery_execution_id") or "") or _latest_recovery_id(
            db, task_id
        )
        run_test_engineer_task(
            db,
            user,
            project_id,
            run_id,
            task_id,
            recovery_execution_id=recovery_id,
            on_execution_started=lambda execution: attach_execution(
                db, job.job_id, worker_id, execution
            ),
        )


def _handle_security(job: WorkflowJob, _worker_id: str) -> None:
    from app.services.security_gate import run_security_gate

    project_id = int(_required_payload(job, "project_id"))
    run_id = _required_payload(job, "run_id")
    code_item_id = _required_payload(job, "code_item_id")
    with SessionLocal() as db:
        run_security_gate(db, project_id=project_id, run_id=run_id, code_item_id=code_item_id)


def _handle_deployment(job: WorkflowJob, _worker_id: str) -> None:
    from app.services.deployment import _deploy_worker

    deployment_id = job.deployment_id or _required_payload(job, "deployment_id")
    _deploy_worker(deployment_id)
    with SessionLocal() as db:
        from app.models.deployment import Deployment

        deployment = db.get(Deployment, deployment_id)
        if deployment is None or deployment.status != "ready":
            raise RuntimeError(deployment.error if deployment is not None else "发布记录不存在")


def execute_job(job_id: str, worker_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        if job is None or job.status != "running" or job.lease_owner != worker_id:
            raise ConflictException("工作任务租约已失效")
        job_type = job.job_type
        db.expunge(job)
    handlers = {
        "product_manager": _handle_product_manager,
        "architect": _handle_architect,
        "engineering": _handle_engineering,
        "quality": _handle_quality,
        "security": _handle_security,
        "deployment": _handle_deployment,
    }
    handlers[job_type](job, worker_id)


class DurableWorker:
    def __init__(self, *, worker_id: str | None = None) -> None:
        identity = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        self.worker_id = worker_id or identity
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_recovery = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever,
            name=f"forgeai-worker-{self.worker_id[-8:]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def run_forever(self) -> None:
        logger.info("durable worker started worker_id=%s", self.worker_id)
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                logger.exception("durable worker loop failed worker_id=%s", self.worker_id)
                worked = False
            if not worked:
                self._stop.wait(settings.workflow_poll_interval_seconds)
        logger.info("durable worker stopped worker_id=%s", self.worker_id)

    def run_once(self) -> bool:
        now = time.monotonic()
        if now - self._last_recovery >= settings.workflow_recovery_interval_seconds:
            with SessionLocal() as db:
                recover_expired_jobs(db)
                recover_unapplied_decisions(db)
                recover_orphaned_work(db)
            self._last_recovery = now
        with SessionLocal() as db:
            job = claim_next_job(db, self.worker_id)
            if job is None:
                return False
            job_id = job.job_id

        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(settings.workflow_job_heartbeat_seconds):
                try:
                    with SessionLocal() as heartbeat_db:
                        heartbeat_job(heartbeat_db, job_id, self.worker_id)
                except Exception:
                    logger.exception("workflow heartbeat failed job_id=%s", job_id)
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"forgeai-heartbeat-{job_id[-8:]}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            execute_job(job_id, self.worker_id)
            with SessionLocal() as db:
                complete_job(db, job_id, self.worker_id)
        except Exception as exc:
            if isinstance(exc, AppException):
                logger.warning("workflow job stopped job_id=%s error=%s", job_id, exc)
            else:
                logger.exception("workflow job failed job_id=%s", job_id)
            with SessionLocal() as db:
                fail_job(db, job_id, self.worker_id, exc)
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
        return True


_embedded_workers: list[DurableWorker] = []
_embedded_workers_lock = threading.Lock()


def start_embedded_worker() -> None:
    with _embedded_workers_lock:
        if _embedded_workers:
            return
        concurrency = max(1, min(settings.workflow_embedded_worker_concurrency, 8))
        workers = [
            DurableWorker(worker_id=f"embedded-{os.getpid()}-{index + 1}-{uuid4().hex[:8]}")
            for index in range(concurrency)
        ]
        for worker in workers:
            worker.start()
        _embedded_workers.extend(workers)
        logger.info("durable worker pool started concurrency=%s", concurrency)


def stop_embedded_worker() -> None:
    with _embedded_workers_lock:
        workers = list(_embedded_workers)
        _embedded_workers.clear()
    for worker in workers:
        worker.stop()
    if workers:
        logger.info("durable worker pool stopped concurrency=%s", len(workers))


def main() -> None:
    worker = DurableWorker()
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()


if __name__ == "__main__":
    main()
