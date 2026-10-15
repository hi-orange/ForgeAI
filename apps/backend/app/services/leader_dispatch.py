"""Lease and checkpoint one Leader turn against an exact approved app_spec."""

from copy import deepcopy
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException
from app.models.leader_dispatch_checkpoint import LeaderDispatchCheckpoint
from app.models.user import User
from app.services.task_execution import EXECUTION_LEASE, lock_run, utc_now


def claim_dispatch(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    approved_item_id: str,
    *,
    frozen_input: dict[str, Any],
) -> tuple[LeaderDispatchCheckpoint, str]:
    try:
        run = lock_run(db, user, project_id, run_id)
        if run.status != "running" or run.active_slot != 1 or run.stage != "pm" or run.paused_at:
            raise ConflictException("当前构建不能开始需求分派")
        saved = db.scalar(
            select(LeaderDispatchCheckpoint)
            .where(LeaderDispatchCheckpoint.approved_item_id == approved_item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        now = utc_now()
        if saved is None:
            saved = LeaderDispatchCheckpoint(
                approved_item_id=approved_item_id,
                build_run_id=run_id,
                frozen_input=deepcopy(frozen_input),
                tool_loop={},
            )
            db.add(saved)
        elif saved.build_run_id != run_id:
            raise ConflictException("Leader 检查点不属于当前构建")
        elif saved.lease_owner and saved.lease_expires_at and saved.lease_expires_at > now:
            raise ConflictException("Leader 正在分派，请勿重复启动")
        owner = f"ldr_{uuid4().hex}"
        saved.lease_owner = owner
        saved.lease_expires_at = now + EXECUTION_LEASE
        db.commit()
        db.refresh(saved)
        return saved, owner
    except Exception:
        db.rollback()
        raise


def require_dispatch(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    approved_item_id: str,
    owner: str,
) -> LeaderDispatchCheckpoint:
    """Hold the run lock through checkpoint writes or deterministic handoff."""
    run = lock_run(db, user, project_id, run_id)
    saved = db.scalar(
        select(LeaderDispatchCheckpoint)
        .where(LeaderDispatchCheckpoint.approved_item_id == approved_item_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        run.status != "running"
        or run.active_slot != 1
        or run.stage != "pm"
        or run.paused_at
        or saved is None
        or saved.build_run_id != run_id
        or saved.lease_owner != owner
        or saved.lease_expires_at is None
        or saved.lease_expires_at <= utc_now()
    ):
        raise ConflictException("Leader 执行已失效，请刷新进度")
    return saved


def save_dispatch(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    approved_item_id: str,
    owner: str,
    progress: dict[str, Any],
) -> None:
    try:
        saved = require_dispatch(db, user, project_id, run_id, approved_item_id, owner)
        saved.tool_loop = deepcopy(progress)
        saved.lease_expires_at = utc_now() + EXECUTION_LEASE
        db.commit()
    except Exception:
        db.rollback()
        raise


def release_dispatch(db: Session, approved_item_id: str, owner: str) -> None:
    from sqlalchemy import update

    db.execute(
        update(LeaderDispatchCheckpoint)
        .where(
            LeaderDispatchCheckpoint.approved_item_id == approved_item_id,
            LeaderDispatchCheckpoint.lease_owner == owner,
        )
        .values(lease_owner=None, lease_expires_at=None)
    )
    db.commit()
