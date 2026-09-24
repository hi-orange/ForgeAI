"""Leader coordination across role boundaries."""

from __future__ import annotations

import logging
import threading
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.models.task import Task
from app.models.user import User
from app.orchestration.architect import run_architecture_task
from app.services import leader as leader_service
from app.services.engineering import claim_code_engineer_task, start_claimed_engineering

logger = logging.getLogger("forgeai")

_active_architecture_tasks: set[str] = set()
_active_architecture_lock = threading.Lock()


def start_dispatched_delivery(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task: Task,
) -> dict[str, Any]:
    """Start the role selected by Leader without another browser command.

    The approval request only persists the assignment. Long-running role work is
    owned by the backend process so closing or refreshing the page cannot strand
    the SOP between two roles.
    """

    if task.recipient == "Code Engineer":
        code_task, execution = claim_code_engineer_task(db, user, project_id, run_id, task.task_id)
        return start_claimed_engineering(
            db,
            user,
            project_id,
            run_id,
            code_task,
            execution,
        )
    if task.recipient != "Architect":
        raise ValueError(f"Leader 分派了不支持自动启动的角色：{task.recipient}")

    with _active_architecture_lock:
        if task.task_id in _active_architecture_tasks:
            return {"started": False, "task_id": task.task_id, "already_running": True}
        _active_architecture_tasks.add(task.task_id)

    if db.new or db.dirty or db.deleted:
        db.commit()
    bind = db.get_bind()
    user_id = user.id
    task_id = task.task_id

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
                    logger.error("architecture task owner missing task_id=%s", task_id)
                    return
                run_architect_and_continue(
                    session,
                    owner,
                    project_id,
                    run_id,
                    task_id,
                )
        except Exception:
            logger.exception("architecture delivery failed task_id=%s", task_id)
        finally:
            with _active_architecture_lock:
                _active_architecture_tasks.discard(task_id)

    threading.Thread(
        target=worker,
        daemon=True,
        name=f"forgeai-architect-{task_id[:12]}",
    ).start()
    return {"started": True, "task_id": task_id}


def run_architect_and_continue(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    recovery_execution_id: str | None = None,
) -> Task:
    """Run Architect, receive its result, then let Leader assign Code Engineer."""

    design_item = run_architecture_task(
        db,
        user,
        project_id,
        run_id,
        task_id,
        recovery_execution_id=recovery_execution_id,
    )
    delivery = leader_service.dispatch_completed_design(
        db, user, project_id, run_id, design_item.item_id
    )
    code_task, code_execution = claim_code_engineer_task(
        db, user, project_id, run_id, delivery.task_id
    )
    start_claimed_engineering(
        db,
        user,
        project_id,
        run_id,
        code_task,
        code_execution,
    )
    return code_task
