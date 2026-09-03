from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.models.build_run import ACTIVE_BUILD_RUN_STATUSES, BuildRun, BuildRunStatus
from app.models.configuration_item import (
    ConfigurationItem,
    ConfigurationItemState,
    ConfigurationItemType,
)
from app.models.project import Project
from app.schemas.configuration_item import ConfigurationItemRegistration

_REQUIRED_UPSTREAM_TYPE = {
    ConfigurationItemType.SYSTEM_DESIGN: ConfigurationItemType.APP_SPEC,
    ConfigurationItemType.CODE: ConfigurationItemType.SYSTEM_DESIGN,
    ConfigurationItemType.TEST_REPORT: ConfigurationItemType.CODE,
}


def _lock_project(db: Session, project_id: int) -> Project:
    project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if project is None:
        raise NotFoundException("项目不存在")
    return project


def _get_build_run(db: Session, project_id: int, run_id: str) -> BuildRun:
    build_run = db.scalar(
        select(BuildRun).where(
            BuildRun.project_id == project_id,
            BuildRun.run_id == run_id,
        )
    )
    if build_run is None:
        raise NotFoundException("构建任务不存在")
    return build_run


def _get_configuration_item(
    db: Session,
    project_id: int,
    item_id: str,
) -> ConfigurationItem:
    item = db.scalar(
        select(ConfigurationItem).where(
            ConfigurationItem.project_id == project_id,
            ConfigurationItem.item_id == item_id,
        )
    )
    if item is None:
        raise NotFoundException("ConfigurationItem 不存在")
    return item


def _load_upstream_items(
    db: Session,
    project_id: int,
    item_ids: list[str],
) -> list[ConfigurationItem]:
    if not item_ids:
        return []

    items = list(
        db.scalars(
            select(ConfigurationItem).where(
                ConfigurationItem.project_id == project_id,
                ConfigurationItem.item_id.in_(item_ids),
            )
        ).all()
    )
    items_by_id = {item.item_id: item for item in items}
    if len(items_by_id) != len(item_ids):
        raise NotFoundException("上游 ConfigurationItem 不存在或不属于当前项目")
    return [items_by_id[item_id] for item_id in item_ids]


def _validate_upstream_types(
    semantic_type: ConfigurationItemType,
    upstream_items: list[ConfigurationItem],
) -> None:
    if semantic_type == ConfigurationItemType.APP_SPEC:
        if any(
            item.semantic_type != ConfigurationItemType.APP_SPEC.value for item in upstream_items
        ):
            raise BusinessException("app_spec 只能引用此前的 app_spec")
        return

    required_type = _REQUIRED_UPSTREAM_TYPE[semantic_type]
    if not upstream_items:
        raise BusinessException(f"{semantic_type.value} 必须引用 {required_type.value}")
    if any(item.semantic_type != required_type.value for item in upstream_items):
        raise BusinessException(f"{semantic_type.value} 的直接上游必须全部是 {required_type.value}")


def _normalize_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise BusinessException("ConfigurationItem 内容必须是有效 JSON") from exc

    normalized = cast(dict[str, Any], json.loads(serialized))
    content_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return normalized, content_hash


def _next_version(
    db: Session,
    project_id: int,
    semantic_type: ConfigurationItemType,
) -> int:
    value = db.scalar(
        select(func.coalesce(func.max(ConfigurationItem.version), 0) + 1).where(
            ConfigurationItem.project_id == project_id,
            ConfigurationItem.semantic_type == semantic_type.value,
        )
    )
    return int(value or 1)


def _unusable_reason(
    build_run: BuildRun,
    upstream_items: list[ConfigurationItem],
) -> str | None:
    reasons: list[str] = []
    if build_run.status != BuildRunStatus.RUNNING.value or build_run.active_slot != 1:
        reasons.append(f"producer_run_not_eligible:{build_run.status}")

    unusable_upstream_ids = [
        item.item_id
        for item in upstream_items
        if item.state == ConfigurationItemState.UNUSABLE.value
    ]
    if unusable_upstream_ids:
        shown_ids = unusable_upstream_ids[:5]
        suffix = (
            f":+{len(unusable_upstream_ids) - len(shown_ids)}"
            if len(unusable_upstream_ids) > 5
            else ""
        )
        reasons.append(f"upstream_unusable:{','.join(shown_ids)}{suffix}")
    return ";".join(reasons) or None


def register_configuration_item(
    db: Session,
    *,
    project_id: int,
    producer_run_id: str,
    submission: ConfigurationItemRegistration,
) -> ConfigurationItem:
    """校验并登记一个新版本；旧运行的晚到结果只会以 unusable 状态留档。"""

    _lock_project(db, project_id)
    build_run = _get_build_run(db, project_id, producer_run_id)
    upstream_items = _load_upstream_items(db, project_id, submission.upstream_item_ids)
    _validate_upstream_types(submission.semantic_type, upstream_items)
    payload, content_hash = _normalize_payload(submission.payload)
    unusable_reason = _unusable_reason(build_run, upstream_items)

    item = ConfigurationItem(
        item_id=f"ci_{uuid4().hex}",
        project_id=project_id,
        producer_run_id=producer_run_id,
        semantic_type=submission.semantic_type.value,
        version=_next_version(db, project_id, submission.semantic_type),
        schema_version=submission.schema_version,
        payload=payload,
        content_hash=content_hash,
        upstream_item_ids=list(submission.upstream_item_ids),
        state=(
            ConfigurationItemState.UNUSABLE.value
            if unusable_reason
            else ConfigurationItemState.USABLE.value
        ),
        unusable_reason=unusable_reason,
        unusable_at=datetime.now(UTC).replace(tzinfo=None) if unusable_reason else None,
    )
    db.add(item)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("ConfigurationItem 版本登记冲突，请重试") from exc

    db.refresh(item)
    return item


def get_configuration_item(
    db: Session,
    *,
    project_id: int,
    item_id: str,
) -> ConfigurationItem:
    return _get_configuration_item(db, project_id, item_id)


def require_usable_configuration_item(
    db: Session,
    *,
    project_id: int,
    item_id: str,
) -> ConfigurationItem:
    item = _get_configuration_item(db, project_id, item_id)
    if item.state == ConfigurationItemState.UNUSABLE.value:
        raise ConflictException("ConfigurationItem 已过期，不能继续使用")
    return item


def mark_run_configuration_items_unusable(
    db: Session,
    *,
    project_id: int,
    producer_run_id: str,
    reason: str,
) -> int:
    """在运行失去资格后，将它此前登记的成果单向标记为 unusable。"""

    normalized_reason = reason.strip()
    if not normalized_reason or len(normalized_reason) > 500:
        raise BusinessException("不可用原因长度必须在 1 到 500 个字符之间")

    _lock_project(db, project_id)
    build_run = _get_build_run(db, project_id, producer_run_id)
    if build_run.status in ACTIVE_BUILD_RUN_STATUSES:
        raise ConflictException("构建任务仍有提交资格，不能标记其成果过期")

    items = list(
        db.scalars(
            select(ConfigurationItem).where(
                ConfigurationItem.project_id == project_id,
                ConfigurationItem.producer_run_id == producer_run_id,
                ConfigurationItem.state == ConfigurationItemState.USABLE.value,
            )
        ).all()
    )
    unusable_at = datetime.now(UTC).replace(tzinfo=None)
    for item in items:
        item.state = ConfigurationItemState.UNUSABLE.value
        item.unusable_reason = normalized_reason
        item.unusable_at = unusable_at

    db.commit()
    return len(items)
