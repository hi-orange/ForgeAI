from __future__ import annotations

import json
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictException, NotFoundException
from app.core.settings import settings
from app.models.configuration_item import ConfigurationItem, ConfigurationItemType
from app.models.security_report import SecurityReport
from app.schemas.code_artifact import CodeArtifact
from app.schemas.security import SecurityConclusion, SecurityScanPayload
from app.services.source_artifact import ensure_code_workspace
from app.services.workflow_events import append_event
from app.tools.checks import run_check

SECURITY_REPORT_PREFIX = "FORGEAI_SECURITY_REPORT="


def _code_artifact(
    db: Session, *, project_id: int, run_id: str, code_item_id: str
) -> tuple[ConfigurationItem, CodeArtifact]:
    item = db.scalar(
        select(ConfigurationItem).where(
            ConfigurationItem.item_id == code_item_id,
            ConfigurationItem.project_id == project_id,
            ConfigurationItem.producer_run_id == run_id,
            ConfigurationItem.semantic_type == ConfigurationItemType.CODE.value,
            ConfigurationItem.state == "usable",
        )
    )
    if item is None:
        raise NotFoundException("安全检查引用的 code artifact 不存在")
    try:
        return item, CodeArtifact.model_validate(item.payload)
    except ValidationError as exc:
        raise ConflictException("安全检查引用的 code artifact 结构无效") from exc


def _parse_scan_output(output: str) -> SecurityScanPayload:
    for line in reversed(output.splitlines()):
        if line.startswith(SECURITY_REPORT_PREFIX):
            try:
                return SecurityScanPayload.model_validate_json(line[len(SECURITY_REPORT_PREFIX) :])
            except (ValidationError, json.JSONDecodeError) as exc:
                raise RuntimeError("安全扫描器返回了无效报告") from exc
    raise RuntimeError("安全扫描器没有返回结构化报告")


def enqueue_security_gate(
    db: Session,
    *,
    user_id: int,
    project_id: int,
    run_id: str,
    code_item_id: str,
) -> None:
    from app.services.workflow_jobs import enqueue_job

    _, artifact = _code_artifact(
        db, project_id=project_id, run_id=run_id, code_item_id=code_item_id
    )
    enqueue_job(
        db,
        job_type="security",
        idempotency_key=(
            f"security:{code_item_id}:{artifact.source_hash}:{settings.security_scanner_version}"
        ),
        project_id=project_id,
        build_run_id=run_id,
        payload={
            "user_id": user_id,
            "project_id": project_id,
            "run_id": run_id,
            "code_item_id": code_item_id,
            "source_hash": artifact.source_hash,
            "scanner_version": settings.security_scanner_version,
        },
    )


def run_security_gate(
    db: Session, *, project_id: int, run_id: str, code_item_id: str
) -> SecurityReport:
    _, artifact = _code_artifact(
        db, project_id=project_id, run_id=run_id, code_item_id=code_item_id
    )
    existing = db.scalar(
        select(SecurityReport).where(
            SecurityReport.code_item_id == code_item_id,
            SecurityReport.scanner_version == settings.security_scanner_version,
        )
    )
    if existing is not None:
        if existing.source_hash != artifact.source_hash:
            raise ConflictException("安全报告与 code artifact 的 source_hash 不一致")
        return existing
    workspace, restored = ensure_code_workspace(
        db, project_id=project_id, run_id=run_id, code_item_id=code_item_id
    )
    if restored:
        db.commit()
    result = run_check(workspace, check_id="security", tool_call_id=f"security:{code_item_id}")
    if result.error_code not in {None, "SECURITY_CHECK_FAILED"}:
        raise RuntimeError(result.summary or "独立安全检查执行失败")
    payload = _parse_scan_output(str(result.data.get("output") or ""))
    if payload.scanner_version != settings.security_scanner_version:
        raise RuntimeError("安全扫描器版本与平台配置不一致")
    if result.data.get("source_hash") != artifact.source_hash:
        raise ConflictException("安全检查没有绑定当前 code artifact")
    report = SecurityReport(
        report_id=f"sec_{uuid4().hex}",
        project_id=project_id,
        build_run_id=run_id,
        code_item_id=code_item_id,
        source_hash=artifact.source_hash,
        scanner_version=payload.scanner_version,
        conclusion=payload.conclusion.value,
        findings=[finding.model_dump(mode="json") for finding in payload.findings],
    )
    try:
        with db.begin_nested():
            db.add(report)
            db.flush()
    except IntegrityError:
        concurrent_report = db.scalar(
            select(SecurityReport).where(
                SecurityReport.code_item_id == code_item_id,
                SecurityReport.scanner_version == settings.security_scanner_version,
            )
        )
        if concurrent_report is None:
            raise
        report = concurrent_report
    append_event(
        db,
        idempotency_key=f"security-report:{report.report_id}",
        stream_key=f"run:{run_id}",
        event_type="security_report_published",
        project_id=project_id,
        build_run_id=run_id,
        payload={
            "report_id": report.report_id,
            "code_item_id": code_item_id,
            "source_hash": artifact.source_hash,
            "scanner_version": report.scanner_version,
            "conclusion": report.conclusion,
            "finding_count": len(report.findings),
        },
    )
    db.commit()
    db.refresh(report)
    return report


def require_security_pass(
    db: Session, *, project_id: int, run_id: str, code_item_id: str
) -> SecurityReport:
    _, artifact = _code_artifact(
        db, project_id=project_id, run_id=run_id, code_item_id=code_item_id
    )
    report = db.scalar(
        select(SecurityReport).where(
            SecurityReport.project_id == project_id,
            SecurityReport.build_run_id == run_id,
            SecurityReport.code_item_id == code_item_id,
            SecurityReport.source_hash == artifact.source_hash,
            SecurityReport.scanner_version == settings.security_scanner_version,
        )
    )
    if report is None:
        raise ConflictException("独立安全扫描尚未完成，当前版本不能发布")
    if report.conclusion != SecurityConclusion.PASSED.value:
        raise ConflictException("独立安全门禁未通过，当前版本不能发布")
    return report


def latest_security_report(db: Session, project_id: int) -> SecurityReport | None:
    return db.scalar(
        select(SecurityReport)
        .where(SecurityReport.project_id == project_id)
        .order_by(SecurityReport.created_at.desc())
        .limit(1)
    )
