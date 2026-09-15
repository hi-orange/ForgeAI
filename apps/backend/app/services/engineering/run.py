"""Start the engineering coding loop after a SoftwareEngineer task is claimed."""

from __future__ import annotations

import logging
import threading
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.models.user import User
from app.orchestration.software_engineer import run_engineering_workflow
from app.services.engineering.claim import read_frozen_input_snapshot
from app.services.task_execution import latest_execution, renew_execution_lease, utc_now

logger = logging.getLogger("forgeai")

_active_executions: set[str] = set()
_active_lock = threading.Lock()


def is_engineering_active(execution_id: str) -> bool:
    with _active_lock:
        return execution_id in _active_executions


def _record_loop_failure(
    session_factory: Any,
    task_id: str,
    execution_id: str,
    message: str,
) -> None:
    try:
        with session_factory() as db:
            execution = latest_execution(db, task_id, lock=True)
            if execution is None or execution.execution_id != execution_id:
                return
            if execution.status != "running" or execution.active_slot != 1:
                return
            if execution.expires_at <= utc_now():
                return
            snapshot = read_frozen_input_snapshot(execution)
            if snapshot is None:
                return
            checkpoint = snapshot.get("checkpoint")
            if not isinstance(checkpoint, dict):
                checkpoint = {
                    "kind": "engineering_checkpoint",
                    "schema_version": 1,
                    "work_items": [],
                    "activity": [],
                    "outcome": "running",
                }
            checkpoint["last_error"] = message[:500]
            activity = list(checkpoint.get("activity") or [])
            activity.append(
                {
                    "id": f"error_{len(activity) + 1}",
                    "name": "error",
                    "label": "Blocked",
                    "detail": message[:200],
                    "ok": False,
                }
            )
            checkpoint["activity"] = activity[-80:]
            snapshot["checkpoint"] = checkpoint
            import json

            from sqlalchemy.orm.attributes import flag_modified

            execution.draft = json.loads(json.dumps(snapshot))
            flag_modified(execution, "draft")
            db.commit()
            renew_execution_lease(db, task_id, execution_id)
    except Exception:
        logger.exception("failed to persist engineering loop error execution_id=%s", execution_id)


def start_claimed_engineering(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task: Task,
    execution: TaskExecution,
    *,
    wait: bool = False,
) -> dict:
    """Run the coding loop. Default is a background thread so the chat can poll tool steps."""
    if wait:
        return run_engineering_workflow(
            db,
            user,
            project_id,
            run_id,
            task.task_id,
            execution.execution_id,
        )
    if db.new or db.dirty or db.deleted:
        db.commit()
    bind = db.get_bind()
    user_id = user.id
    task_id = task.task_id
    execution_id = execution.execution_id

    with _active_lock:
        if execution_id in _active_executions:
            return {"started": False, "execution_id": execution_id, "already_running": True}
        _active_executions.add(execution_id)

    def worker() -> None:
        factory = sessionmaker(
            bind=bind,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        try:
            with factory() as session:
                owner = session.get(User, user_id)
                if owner is None:
                    _record_loop_failure(factory, task_id, execution_id, "工程执行用户不存在")
                    return
                run_engineering_workflow(
                    session,
                    owner,
                    project_id,
                    run_id,
                    task_id,
                    execution_id,
                    session_factory=factory,
                )
        except Exception as exc:
            logger.exception("engineering loop failed execution_id=%s", execution_id)
            _record_loop_failure(factory, task_id, execution_id, str(exc) or "工程循环失败")
        finally:
            with _active_lock:
                _active_executions.discard(execution_id)

    threading.Thread(target=worker, daemon=True, name=f"forgeai-eng-{execution_id[:12]}").start()
    return {"started": True, "execution_id": execution_id}
