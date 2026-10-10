from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.agent_decision_event import AgentDecisionEvent
from app.models.agent_eval_label import AgentEvalLabel
from app.models.agent_usage_event import AgentUsageEvent
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.project_message_classification import ProjectMessageClassification
from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.models.user import User
from app.schemas.evaluation import (
    AgentEvalLabelCreate,
    AgentEvaluationReport,
    AgentUsageSummary,
    EvalMetric,
)
from app.schemas.leader import NextAction, NextActionKind
from app.schemas.test_report import QualityConclusion, TestReport
from app.services import project as project_service
from app.services.project_message_classification import require_project_message


def _rate(name: str, numerator: int, denominator: int, *, note: str | None = None) -> EvalMetric:
    return EvalMetric(
        name=name,
        status="available",
        numerator=numerator,
        denominator=denominator,
        value=(numerator / denominator if denominator else None),
        note=note if denominator else (note or "尚无可评估样本"),
    )


def _routing_contract_pass(event: AgentDecisionEvent) -> bool:
    try:
        action = NextAction.model_validate(event.decision_payload)
    except ValidationError:
        return False
    if action.action.value != event.action or action.reason_code != event.reason_code:
        return False
    expected = {
        NextActionKind.DISPATCH_PRODUCT_MANAGER: "Product Manager",
        NextActionKind.DISPATCH_ARCHITECT: "Architect",
        NextActionKind.DISPATCH_CODE_ENGINEER: "Code Engineer",
    }.get(action.action)
    return event.recipient == expected


def _test_reports(db: Session, project_id: int) -> list[TestReport]:
    reports: list[TestReport] = []
    for payload in db.scalars(
        select(ConfigurationItem.payload).where(
            ConfigurationItem.project_id == project_id,
            ConfigurationItem.semantic_type == "test_report",
            ConfigurationItem.state == "usable",
        )
    ).all():
        try:
            reports.append(TestReport.model_validate(payload))
        except ValidationError:
            continue
    return reports


def evaluate_project(db: Session, user: User, project_id: int) -> AgentEvaluationReport:
    project_service.get_user_project(db, user, project_id)
    runs = list(db.scalars(select(BuildRun).where(BuildRun.project_id == project_id)).all())
    terminal = [run for run in runs if run.status in {"succeeded", "failed", "cancelled"}]
    succeeded = sum(run.status == "succeeded" for run in terminal)

    reports = _test_reports(db, project_id)
    passed_reports = sum(
        report.quality_conclusion == QualityConclusion.PASSED for report in reports
    )

    tasks = list(
        db.scalars(
            select(Task)
            .join(Plan, Task.plan_id == Plan.plan_id)
            .where(Plan.project_id == project_id)
        ).all()
    )
    task_ids = [task.task_id for task in tasks]
    executions = (
        list(db.scalars(select(TaskExecution).where(TaskExecution.task_id.in_(task_ids))).all())
        if task_ids
        else []
    )
    completed_task_ids = {task.task_id for task in tasks if task.status == "succeeded"}
    first_attempt_completed = {
        execution.task_id
        for execution in executions
        if execution.attempt == 1
        and execution.status == "succeeded"
        and execution.task_id in completed_task_ids
    }
    retry_attempts = [execution for execution in executions if execution.attempt > 1]

    decisions = list(
        db.scalars(
            select(AgentDecisionEvent).where(AgentDecisionEvent.project_id == project_id)
        ).all()
    )
    contract_passes = sum(_routing_contract_pass(event) for event in decisions)
    applied = sum(event.status == "applied" for event in decisions)
    classified_count = int(
        db.scalar(
            select(func.count())
            .select_from(ProjectMessageClassification)
            .join(
                AgentDecisionEvent,
                AgentDecisionEvent.message_id == ProjectMessageClassification.message_id,
                isouter=True,
            )
            .where(AgentDecisionEvent.project_id == project_id)
        )
        or 0
    )
    unnecessary_leader = sum(
        event.decided_by == "leader" and event.category in {"inquiry", "stop", "product_change"}
        for event in decisions
    )
    leader_decisions = sum(event.decided_by == "leader" for event in decisions)
    labels = list(
        db.scalars(select(AgentEvalLabel).where(AgentEvalLabel.project_id == project_id)).all()
    )
    classifications = (
        {
            row.message_id: row.category
            for row in db.scalars(
                select(ProjectMessageClassification).where(
                    ProjectMessageClassification.message_id.in_(
                        [label.message_id for label in labels]
                    )
                )
            ).all()
        }
        if labels
        else {}
    )
    decisions_by_message = {
        event.message_id: event for event in decisions if event.message_id is not None
    }
    classification_correct = sum(
        classifications.get(label.message_id) == label.expected_category for label in labels
    )
    routing_labels = [label for label in labels if label.expected_action is not None]
    routing_correct = sum(
        decisions_by_message.get(label.message_id) is not None
        and decisions_by_message[label.message_id].action == label.expected_action
        for label in routing_labels
    )

    usages = list(
        db.scalars(select(AgentUsageEvent).where(AgentUsageEvent.project_id == project_id)).all()
    )
    prompt_values = [item.prompt_tokens for item in usages if item.prompt_tokens is not None]
    completion_values = [
        item.completion_tokens for item in usages if item.completion_tokens is not None
    ]
    total_values = [item.total_tokens for item in usages if item.total_tokens is not None]
    cost_values = [
        item.estimated_cost_microusd for item in usages if item.estimated_cost_microusd is not None
    ]
    durations = [max(0.0, (run.updated_at - run.created_at).total_seconds()) for run in terminal]

    metrics = [
        _rate("end_to_end_task_success", succeeded, len(terminal)),
        _rate("acceptance_pass", passed_reports, len(reports)),
        _rate(
            "schema_and_execution_first_pass",
            len(first_attempt_completed),
            len(completed_task_ids),
            note="以结构化输出通过并在首次 TaskExecution 完成作为可审计的一次通过口径。",
        ),
        _rate("routing_contract_pass", contract_passes, len(decisions)),
        _rate(
            "unnecessary_leader_invocation",
            unnecessary_leader,
            leader_decisions,
            note="值越低越好；只统计本可由确定性引擎处理的类别。",
        ),
        EvalMetric(
            name="average_repair_rounds",
            status="available",
            numerator=len(retry_attempts),
            denominator=len(terminal),
            value=(len(retry_attempts) / len(terminal) if terminal else None),
            unit="rounds_per_run",
            note=None if terminal else "尚无终态运行",
        ),
        _rate("regression_pass", passed_reports, len(reports)),
        _rate(
            "trajectory_complete",
            applied,
            len(decisions),
            note=(f"已有 {classified_count} 条带决策事件的分类消息；failed 决策仍保留错误证据。"),
        ),
        _rate(
            "replayable_decision",
            sum(bool(event.decision_payload) and bool(event.decision_key) for event in decisions),
            len(decisions),
        ),
        EvalMetric(
            name="average_end_to_end_seconds",
            status="available",
            numerator=None,
            denominator=len(durations),
            value=(sum(durations) / len(durations) if durations else None),
            unit="seconds",
            note=None if durations else "尚无终态运行",
        ),
        (
            _rate("message_classification_accuracy", classification_correct, len(labels))
            if labels
            else EvalMetric(
                name="message_classification_accuracy",
                status="unavailable",
                note="尚未导入人工或固定数据集标签。",
            )
        ),
        (
            _rate("routing_accuracy", routing_correct, len(routing_labels))
            if routing_labels
            else EvalMetric(
                name="routing_accuracy",
                status="unavailable",
                note="尚未导入带 expected_action 的路由标签，不能用规则自证准确率。",
            )
        ),
    ]
    return AgentEvaluationReport(
        project_id=project_id,
        generated_at=datetime.now(UTC),
        metrics=metrics,
        usage=AgentUsageSummary(
            prompt_tokens=sum(prompt_values) if prompt_values else None,
            completion_tokens=sum(completion_values) if completion_values else None,
            total_tokens=sum(total_values) if total_values else None,
            duration_ms=sum(item.duration_ms for item in usages),
            estimated_cost_microusd=sum(cost_values) if cost_values else None,
            model_turns=len(usages),
        ),
    )


def save_eval_label(
    db: Session,
    user: User,
    project_id: int,
    payload: AgentEvalLabelCreate,
) -> AgentEvalLabel:
    project_service.get_user_project(db, user, project_id)
    require_project_message(db, project_id, payload.message_id)
    existing = db.scalar(
        select(AgentEvalLabel).where(AgentEvalLabel.message_id == payload.message_id)
    )
    if existing is None:
        existing = AgentEvalLabel(
            label_id=f"lbl_{uuid4().hex}",
            project_id=project_id,
            message_id=payload.message_id,
            expected_category=payload.expected_category.value,
            expected_action=payload.expected_action.value if payload.expected_action else None,
            labeled_by_user_id=user.id,
        )
        db.add(existing)
    else:
        existing.expected_category = payload.expected_category.value
        existing.expected_action = (
            payload.expected_action.value if payload.expected_action else None
        )
        existing.labeled_by_user_id = user.id
    db.commit()
    db.refresh(existing)
    return existing
