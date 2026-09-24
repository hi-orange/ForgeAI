"""Run one assigned Test Engineer task against an exact code identity."""

from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.agents.test_engineer import verify_code
from app.core.settings import settings
from app.generation.workspace import default_workspace_path
from app.models.configuration_item import ConfigurationItem
from app.models.task_execution import TaskExecution
from app.models.user import User
from app.services import test_engineer as test_engineer_service
from app.services.task_execution import fail_execution

logger = logging.getLogger("forgeai")


def _record_quality_activity(
    db: Session,
    execution: TaskExecution,
    *,
    name: str,
    label: str,
    detail: str,
    ok: bool = True,
) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    draft = dict(execution.draft or {})
    checkpoint = draft.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("kind") != "quality_checkpoint":
        checkpoint = {"kind": "quality_checkpoint", "activity": []}
    activity = list(checkpoint.get("activity") or [])
    activity.append(
        {
            "id": f"{name}_{len(activity) + 1}",
            "name": name,
            "label": label,
            "detail": detail[:200],
            "ok": ok,
        }
    )
    checkpoint["activity"] = activity
    draft["checkpoint"] = checkpoint
    # JSON columns need an explicit dirty flag or later activity rows never persist.
    execution.draft = json.loads(json.dumps(draft, ensure_ascii=False))
    flag_modified(execution, "draft")
    db.commit()
    db.refresh(execution)


def run_test_engineer_task(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    *,
    recovery_execution_id: str | None = None,
) -> ConfigurationItem:
    """Verify code read-only, then publish the immutable quality report."""

    task, execution = test_engineer_service.claim_test_engineer_task(
        db,
        user,
        project_id,
        run_id,
        task_id,
        recovery_execution_id=recovery_execution_id,
    )
    _record_quality_activity(
        db,
        execution,
        name="quality_start",
        label="开始独立验证",
        detail="检查准确代码结果并逐条验证批准的验收条件",
    )
    with Session(bind=db.get_bind(), autoflush=False) as reader:
        inputs = test_engineer_service.load_test_inputs(
            reader,
            project_id,
            run_id,
            task.input_configuration_item_ids[0],
        )
    workspace_root = default_workspace_path(settings.runtime_data_root, project_id, run_id)
    try:
        report = verify_code(
            spec=inputs.app_spec,
            system_design=inputs.system_design,
            code_item_id=inputs.code_item.item_id,
            code_source_hash=inputs.code_artifact.source_hash,
            workspace_root=workspace_root,
        )
        _record_quality_activity(
            db,
            execution,
            name="quality_result",
            label="独立验证完成",
            detail=report.summary,
            ok=report.quality_conclusion.value == "passed",
        )
        report_item = test_engineer_service.complete_test_engineer_task(
            db,
            user,
            project_id,
            run_id,
            task_id,
            execution.execution_id,
            report,
        )
    except Exception as exc:
        message = str(exc).strip() or "Test Engineer 执行失败，可重试恢复。"
        _record_quality_activity(
            db,
            execution,
            name="quality_error",
            label="独立验证失败",
            detail=message,
            ok=False,
        )
        fail_execution(
            db,
            task_id,
            execution.execution_id,
            error=message[:500],
        )
        raise

    if report.quality_conclusion.value == "failed":
        # Role output is routed by Leader. Test Engineer remains read-only and
        # the report becomes the exact structured input of the repair task.
        from app.services import engineering
        from app.services import leader as leader_service

        try:
            repair_task = leader_service.dispatch_quality_repair(
                db,
                user,
                project_id,
                run_id,
                report_item.item_id,
            )
            if repair_task is not None:
                code_task, code_execution = engineering.claim_code_engineer_task(
                    db,
                    user,
                    project_id,
                    run_id,
                    repair_task.task_id,
                )
                engineering.start_claimed_engineering(
                    db,
                    user,
                    project_id,
                    run_id,
                    code_task,
                    code_execution,
                )
        except Exception:
            # The immutable report is already published. Never rewrite that
            # successful execution as failed because downstream routing broke.
            logger.exception(
                "quality repair routing failed report_item_id=%s",
                report_item.item_id,
            )
            raise
    return report_item
