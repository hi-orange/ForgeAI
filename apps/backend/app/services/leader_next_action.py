"""Hybrid scheduling: deterministic engine paths + Leader semantic decisions.

Leader proposes ambiguous NextAction values; this module validates hard gates and
applies only legal transitions. Single-path handoffs (Architect→Code, Code→Test)
stay in ``services.leader`` as workflow automation.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import leader as leader_agent
from app.core.exceptions import BusinessException, ConflictException
from app.models.build_run import BuildRun
from app.models.configuration_item import ConfigurationItem, ConfigurationItemType
from app.models.project import Project, ProjectStatus
from app.models.project_message_classification import ProjectMessageCategory
from app.models.task import Task, TaskRecipient
from app.models.task_result import TaskResult
from app.models.user import User
from app.schemas.leader import (
    LeaderOutcome,
    NextAction,
    NextActionKind,
)
from app.schemas.test_report import QualityConclusion, TestReport
from app.services import decision_event as decision_event_service
from app.services import leader as leader_service
from app.services import plan as plan_service
from app.services import project as project_service
from app.services import project_message as project_message_service
from app.services import run_control
from app.services import run_revision as run_revision_service
from app.services.engineering import APPROVAL_VERSION
from app.services.project_message_classification import require_project_message
from app.telemetry.agent import agent_telemetry_scope

MESSAGE_TURN_INSTRUCTION = """
本轮只做用户消息的语义调度。先 read_project_context，再 classify_intent。
规则：
1. inquiry：finish_turn(action="finish") 直接回答或说明，不创建交付计划。
2. stop：只取消用户明确要求停止的工作；若无可取消工作则 finish 说明。
3. product_change：安排 Product Manager 产出待批准 app_spec；批准前不得安排
   Architect、Code Engineer 或 Test Engineer。
4. implementation_repair：仅当上下文中存在已批准 app_spec 时，安排 Code Engineer，
   并在任务中引用准确 item id；没有可修复实现时 finish 说明原因。
5. 信息不足时 request_user_input；它不是批准。
本轮最多创建一个可立即分派的任务。
""".strip()

QUALITY_FAILURE_TURN_INSTRUCTION = """
本轮仲裁一份 FAILED/BLOCKED 的独立验收报告。先 read_project_context 与 read_task_result。
规则：
1. FAILED 是实现未满足冻结意图，派 Code Engineer，并引用准确 code / app_spec / test_report id。
2. BLOCKED 且无测试质疑是环境或证据阻断，应恢复验收基础设施，不要改代码。
3. 冻结测试与获批意图冲突时等待用户 challenge 裁决，不要改测试或擅自修产品。
4. 不得直接派 Test Engineer；其复验由引擎在新代码发布后自动创建。
""".strip()


def _latest_approved_app_spec_item(
    db: Session, *, project_id: int, run_id: str | None = None
) -> ConfigurationItem | None:
    query = (
        select(ConfigurationItem)
        .join(TaskResult, TaskResult.configuration_item_id == ConfigurationItem.item_id)
        .where(
            ConfigurationItem.project_id == project_id,
            ConfigurationItem.semantic_type == ConfigurationItemType.APP_SPEC.value,
            ConfigurationItem.state == "usable",
            TaskResult.prompt_version == APPROVAL_VERSION,
        )
        .order_by(ConfigurationItem.id.desc())
        .limit(1)
    )
    if run_id is not None:
        query = query.where(ConfigurationItem.producer_run_id == run_id)
    return db.scalar(query)


def _project_has_configuration_items(db: Session, project_id: int) -> bool:
    return (
        db.scalar(
            select(ConfigurationItem.item_id)
            .where(ConfigurationItem.project_id == project_id)
            .limit(1)
        )
        is not None
    )


def _active_run(db: Session, project_id: int, run_id: str | None) -> BuildRun | None:
    if not run_id:
        return None
    return db.scalar(
        select(BuildRun).where(BuildRun.project_id == project_id, BuildRun.run_id == run_id)
    )


def resolve_message_next_action(
    db: Session,
    *,
    project_id: int,
    run_id: str | None,
    message_id: int,
    category: str,
) -> NextAction:
    """Return a deterministic NextAction, or NEEDS_LEADER when semantics are ambiguous."""

    require_project_message(db, project_id, message_id)
    project = db.get(Project, project_id)
    if project is None:
        raise BusinessException("项目不存在")
    approved = _latest_approved_app_spec_item(db, project_id=project_id, run_id=run_id)
    if approved is None:
        approved = _latest_approved_app_spec_item(db, project_id=project_id)
    has_items = _project_has_configuration_items(db, project_id)
    run = _active_run(db, project_id, run_id)

    if category == ProjectMessageCategory.INQUIRY.value:
        return NextAction(
            action=NextActionKind.REPLY,
            reason_code="inquiry_no_delivery",
            summary="咨询类消息不启动交付。",
            reply_text="先具体描述一下你想做的应用或功能，我再帮你整理构建计划。",
            decided_by="engine",
        )

    if category == ProjectMessageCategory.STOP.value:
        if run is not None and run.status in {"queued", "running"} and run.active_slot == 1:
            return NextAction(
                action=NextActionKind.CANCEL_ACTIVE_RUN,
                reason_code="user_cancel_active_run",
                summary="用户明确停止当前构建，原子取消运行、计划、任务和执行。",
                risk_flags=["user_cancel"],
                decided_by="engine",
            )
        return NextAction(
            action=NextActionKind.REPLY,
            reason_code="stop_without_active_run",
            summary="停止意图，但当前没有活动构建。",
            reply_text="当前没有正在运行的构建。",
            decided_by="engine",
        )

    if category == ProjectMessageCategory.PRODUCT_CHANGE.value:
        first_build = (
            project.status == ProjectStatus.DRAFT.value
            and not has_items
            and approved is None
            and (run is None or run.status in {"queued", "running"})
        )
        if first_build:
            return NextAction(
                action=NextActionKind.DISPATCH_PRODUCT_MANAGER,
                reason_code="initial_product_change",
                summary="首次产品变更，唯一合法后续是 Product Manager。",
                recipient=TaskRecipient.PRODUCT_MANAGER,
                approval_required=True,
                decided_by="engine",
            )
        return NextAction(
            action=NextActionKind.DISPATCH_PRODUCT_MANAGER,
            reason_code="product_revision_requires_approval",
            summary="产品行为变更创建新 app_spec 版本并重新等待用户批准。",
            recipient=TaskRecipient.PRODUCT_MANAGER,
            source_artifact_ids=[approved.item_id] if approved else [],
            required_inputs=["approved_app_spec", "revision_message"],
            approval_required=True,
            decided_by="engine",
        )

    if category == ProjectMessageCategory.IMPLEMENTATION_REPAIR.value:
        if approved is None:
            return NextAction(
                action=NextActionKind.REPLY,
                reason_code="repair_without_approved_intent",
                summary="没有已批准产品意图，无法进入实现修复。",
                reply_text="现在还没有可修复的实现。先描述你想做的应用，我会整理一份构建计划。",
                decided_by="engine",
            )
        current_run_id = getattr(run, "run_id", None) if run is not None else None
        revision = (
            run_revision_service.get_revision_for_target(db, project_id, current_run_id)
            if current_run_id
            else None
        )
        is_new_repair = (
            revision is not None
            and revision.kind == "implementation_repair"
            and run is not None
            and run.status == "queued"
            and run.active_slot == 1
        )
        if (run is None or run.status != "running" or run.active_slot != 1) and not is_new_repair:
            return NextAction(
                action=NextActionKind.REPLY,
                reason_code="repair_without_active_run",
                summary="没有可承接修复的运行中构建。",
                reply_text=(
                    "当前没有可继续修复的构建。若要改产品行为请说明新需求；"
                    "若刚才验收失败，请使用继续/修复入口。"
                ),
                source_artifact_ids=[approved.item_id],
                decided_by="engine",
            )
        if not is_new_repair and (run is None or run.stage not in {"pm", "developer", "qa"}):
            return NextAction(
                action=NextActionKind.NEEDS_LEADER,
                reason_code="repair_stage_ambiguous",
                summary="修复请求落在非标准阶段，交给 Leader 仲裁。",
                source_artifact_ids=[approved.item_id],
                decided_by="engine",
            )
        return NextAction(
            action=NextActionKind.DISPATCH_CODE_ENGINEER,
            reason_code="implementation_repair",
            summary="已批准意图下的实现修复，派给 Code Engineer。",
            recipient=TaskRecipient.CODE_ENGINEER,
            source_artifact_ids=[approved.item_id],
            required_inputs=["approved_app_spec"],
            decided_by="engine",
        )

    return NextAction(
        action=NextActionKind.REPLY,
        reason_code="unknown_category",
        summary="未知消息类别。",
        reply_text="请描述你要做的应用或功能，我再开始整理需求。",
        decided_by="engine",
    )


def resolve_quality_failure_next_action(
    report: TestReport,
    *,
    approved_item_id: str,
    code_item_id: str,
    report_item_id: str,
    challenge_resolved: bool = False,
) -> NextAction:
    """Classify a failed/blocked acceptance report into a deterministic NextAction."""

    artifacts = [code_item_id, approved_item_id, report_item_id]
    if report.quality_conclusion == QualityConclusion.PASSED:
        raise BusinessException("已通过的质量报告不需要失败分流")

    if challenge_resolved:
        # User already confirmed the frozen test; only bounded code repair remains.
        return NextAction(
            action=NextActionKind.DISPATCH_CODE_ENGINEER,
            reason_code="challenge_resolved_repair",
            summary="用户确认冻结测试后，派给 Code Engineer 有界修复。",
            recipient=TaskRecipient.CODE_ENGINEER,
            source_artifact_ids=artifacts,
            required_inputs=["approved_app_spec", "code", "test_report"],
            risk_flags=["implementation", "challenge_resolved"],
            decided_by="engine",
        )

    if report.test_challenges:
        return NextAction(
            action=NextActionKind.AWAIT_USER_CHALLENGE,
            reason_code="acceptance_test_challenge",
            summary="冻结测试与获批意图冲突，等待用户裁决。",
            source_artifact_ids=artifacts,
            risk_flags=["test_challenge"],
            decided_by="engine",
        )

    if report.quality_conclusion == QualityConclusion.BLOCKED:
        return NextAction(
            action=NextActionKind.RETRY_INFRASTRUCTURE,
            reason_code="acceptance_blocked_environment",
            summary="验收被环境或证据阻断，应重试基础设施而非改代码。",
            source_artifact_ids=artifacts,
            risk_flags=["environment"],
            decided_by="engine",
        )

    return NextAction(
        action=NextActionKind.DISPATCH_CODE_ENGINEER,
        reason_code="implementation_quality_failure",
        summary="普通实现缺陷，派给 Code Engineer 有界修复。",
        recipient=TaskRecipient.CODE_ENGINEER,
        source_artifact_ids=artifacts,
        required_inputs=["approved_app_spec", "code", "test_report"],
        risk_flags=["implementation"],
        decided_by="engine",
    )


def validate_next_action(
    action: NextAction,
    *,
    category: str | None = None,
) -> NextAction:
    """Hard gates that neither Leader nor shortcuts may bypass."""

    if action.action in {
        NextActionKind.NEEDS_LEADER,
        NextActionKind.RETRY_INFRASTRUCTURE,
        NextActionKind.AWAIT_USER_CHALLENGE,
    }:
        return action

    if action.action in {
        NextActionKind.DISPATCH_ARCHITECT,
        NextActionKind.DISPATCH_CODE_ENGINEER,
    }:
        if category == ProjectMessageCategory.PRODUCT_CHANGE.value:
            raise BusinessException("产品变更在用户批准前只能派给 Product Manager")
        if not action.source_artifact_ids:
            raise BusinessException("工程分派必须引用准确成果身份")

    if (
        action.action == NextActionKind.DISPATCH_PRODUCT_MANAGER
        and category == ProjectMessageCategory.IMPLEMENTATION_REPAIR.value
    ):
        raise BusinessException("实现修复不得改派 Product Manager 冒充产品变更")

    if action.action == NextActionKind.DISPATCH_CODE_ENGINEER:
        if (
            category == ProjectMessageCategory.PRODUCT_CHANGE.value
            and not action.source_artifact_ids
        ):
            raise BusinessException("产品变更在批准前不能派给 Code Engineer")
        if action.recipient != TaskRecipient.CODE_ENGINEER:
            raise BusinessException("Code Engineer 分派的 recipient 不一致")

    if action.action == NextActionKind.DISPATCH_PRODUCT_MANAGER:
        if action.recipient != TaskRecipient.PRODUCT_MANAGER:
            raise BusinessException("Product Manager 分派的 recipient 不一致")

    if action.action == NextActionKind.DISPATCH_ARCHITECT:
        if action.recipient != TaskRecipient.ARCHITECT:
            raise BusinessException("Architect 分派的 recipient 不一致")

    return action


def next_action_from_leader_outcome(outcome: LeaderOutcome) -> NextAction:
    """Map a Leader tool-loop outcome onto the platform NextAction contract."""

    if outcome.action == "wait_user":
        return NextAction(
            action=NextActionKind.ASK_USER,
            reason_code="leader_request_user_input",
            summary=outcome.summary,
            question=outcome.question,
            decided_by="leader",
        )
    if outcome.action in {"finish", "cancel"}:
        summary = outcome.summary
        return NextAction(
            action=NextActionKind.REPLY,
            reason_code="leader_finish" if outcome.action == "finish" else "leader_cancel",
            summary=summary,
            reply_text=summary,
            decided_by="leader",
        )
    if outcome.action in {"dispatch", "continue"} and outcome.plan is not None:
        if len(outcome.dispatched_task_keys) != 1:
            raise BusinessException("消息调度本轮只能分派一个任务")
        key = outcome.dispatched_task_keys[0]
        task = next((item for item in outcome.plan.tasks if item.task_key == key), None)
        if task is None:
            raise BusinessException("Leader 分派的任务不在计划中")
        if task.recipient == TaskRecipient.PRODUCT_MANAGER:
            return NextAction(
                action=NextActionKind.DISPATCH_PRODUCT_MANAGER,
                reason_code="leader_dispatch_product_manager",
                summary=outcome.summary,
                recipient=TaskRecipient.PRODUCT_MANAGER,
                source_artifact_ids=list(task.input_configuration_item_ids),
                approval_required=True,
                plan=outcome.plan,
                dispatched_task_key=key,
                decided_by="leader",
            )
        if task.recipient == TaskRecipient.CODE_ENGINEER:
            return NextAction(
                action=NextActionKind.DISPATCH_CODE_ENGINEER,
                reason_code="leader_dispatch_code_engineer",
                summary=outcome.summary,
                recipient=TaskRecipient.CODE_ENGINEER,
                source_artifact_ids=list(task.input_configuration_item_ids),
                plan=outcome.plan,
                dispatched_task_key=key,
                decided_by="leader",
            )
        if task.recipient == TaskRecipient.ARCHITECT:
            return NextAction(
                action=NextActionKind.DISPATCH_ARCHITECT,
                reason_code="leader_dispatch_architect",
                summary=outcome.summary,
                recipient=TaskRecipient.ARCHITECT,
                source_artifact_ids=list(task.input_configuration_item_ids),
                plan=outcome.plan,
                dispatched_task_key=key,
                decided_by="leader",
            )
        if task.recipient == TaskRecipient.TEST_ENGINEER:
            raise BusinessException("用户消息调度轮不得直接派 Test Engineer")
    raise BusinessException("Leader 本轮没有给出可执行的调度结果")


def decide_message_next_action(
    db: Session,
    user: User,
    *,
    project_id: int,
    run_id: str | None,
    message_id: int,
    category: str,
) -> NextAction:
    """Engine-first resolution; call Leader only when the path is ambiguous."""

    project_service.get_user_project(db, user, project_id)
    decision_key = f"message:{message_id}"
    replay = decision_event_service.get_decision(db, decision_key)
    if replay is not None:
        return replay[1]
    proposed = resolve_message_next_action(
        db,
        project_id=project_id,
        run_id=run_id,
        message_id=message_id,
        category=category,
    )
    if proposed.action != NextActionKind.NEEDS_LEADER:
        action = validate_next_action(proposed, category=category)
        return decision_event_service.record_decision(
            db,
            decision_key=decision_key,
            project_id=project_id,
            build_run_id=run_id,
            message_id=message_id,
            category=category,
            action=action,
        )

    if not run_id:
        raise BusinessException("歧义调度需要有效的 BuildRun")
    context = leader_service.build_leader_context(
        db,
        project_id=project_id,
        run_id=run_id,
        target_message_id=message_id,
    )
    with agent_telemetry_scope(
        project_id=project_id,
        build_run_id=run_id,
        task_id=None,
        execution_id=None,
        role="Leader",
        bind=db.get_bind(),
    ):
        outcome = leader_agent.lead_project_turn(
            context,
            instruction=MESSAGE_TURN_INSTRUCTION,
            max_turns=leader_agent.LEADER_SIMPLE_DISPATCH_TURNS,
        )
    mapped = next_action_from_leader_outcome(outcome)
    action = validate_next_action(mapped, category=category)
    return decision_event_service.record_decision(
        db,
        decision_key=decision_key,
        project_id=project_id,
        build_run_id=run_id,
        message_id=message_id,
        category=category,
        action=action,
    )


def decide_quality_failure_next_action(
    db: Session,
    user: User,
    *,
    project_id: int,
    run_id: str,
    report: TestReport,
    approved_item_id: str,
    code_item_id: str,
    report_item_id: str,
    cause_message_id: int,
    challenge_resolved: bool = False,
) -> NextAction:
    """Engine-first quality failure routing; Leader only for mixed/ambiguous owners."""

    project_service.get_user_project(db, user, project_id)
    proposed = resolve_quality_failure_next_action(
        report,
        approved_item_id=approved_item_id,
        code_item_id=code_item_id,
        report_item_id=report_item_id,
        challenge_resolved=challenge_resolved,
    )
    decision_key = f"quality:{report_item_id}:{int(challenge_resolved)}"
    replay = decision_event_service.get_decision(db, decision_key)
    if replay is not None:
        return replay[1]
    if proposed.action != NextActionKind.NEEDS_LEADER:
        return decision_event_service.record_decision(
            db,
            decision_key=decision_key,
            project_id=project_id,
            build_run_id=run_id,
            report_item_id=report_item_id,
            action=validate_next_action(proposed),
        )

    context = leader_service.build_leader_context(
        db,
        project_id=project_id,
        run_id=run_id,
        target_message_id=cause_message_id,
    )
    with agent_telemetry_scope(
        project_id=project_id,
        build_run_id=run_id,
        task_id=None,
        execution_id=None,
        role="Leader",
        bind=db.get_bind(),
    ):
        outcome = leader_agent.lead_project_turn(
            context,
            instruction=(
                f"{QUALITY_FAILURE_TURN_INSTRUCTION}\n"
                f"report_item_id={report_item_id}; code_item_id={code_item_id}; "
                f"approved_item_id={approved_item_id}."
            ),
            max_turns=leader_agent.LEADER_SIMPLE_DISPATCH_TURNS,
        )
    return decision_event_service.record_decision(
        db,
        decision_key=decision_key,
        project_id=project_id,
        build_run_id=run_id,
        report_item_id=report_item_id,
        action=validate_next_action(next_action_from_leader_outcome(outcome)),
    )


def apply_message_next_action(
    db: Session,
    user: User,
    *,
    project_id: int,
    run_id: str,
    message_id: int,
    action: NextAction,
    category: str | None = None,
) -> Task | None:
    """Persist side effects for a validated message NextAction. Returns a task when dispatched."""

    action = validate_next_action(
        NextAction.model_validate(action.model_dump()),
        category=category,
    )
    decision_key = f"message:{message_id}"
    if action.action == NextActionKind.CANCEL_ACTIVE_RUN:
        run_control.cancel_active_run(db, user, project_id, run_id)
        project_message_service.create_assistant_project_message(
            db,
            user,
            project_id,
            "本次构建已取消。已完成的历史版本仍然保留，你可以继续描述下一版修改。",
            client_message_id=f"leader:{action.reason_code}:msg:{message_id}",
        )
        decision_event_service.mark_applied(db, decision_key)
        return None
    if action.action in {
        NextActionKind.REPLY,
        NextActionKind.ASK_USER,
        NextActionKind.RETRY_INFRASTRUCTURE,
        NextActionKind.AWAIT_USER_CHALLENGE,
    }:
        text = (
            action.question
            if action.action == NextActionKind.ASK_USER
            else (action.reply_text or action.summary)
        )
        assert text is not None
        project_message_service.create_assistant_project_message(
            db,
            user,
            project_id,
            text,
            client_message_id=f"leader:{action.reason_code}:msg:{message_id}",
        )
        decision_event_service.mark_applied(db, decision_key)
        return None

    if action.action == NextActionKind.DISPATCH_PRODUCT_MANAGER:
        if action.plan is not None:
            # Persist Leader-authored follow-up requirements plan for PM workflow pickup.
            plan_service.save_plan(db, user, project_id, run_id, action.plan)
        elif run_revision_service.get_revision_for_target(db, project_id, run_id) is not None:
            run_revision_service.create_product_revision_plan(db, user, project_id, run_id)
        decision_event_service.mark_applied(db, decision_key)
        return None

    if action.action == NextActionKind.DISPATCH_CODE_ENGINEER:
        if action.plan is not None:
            plan = plan_service.save_plan(db, user, project_id, run_id, action.plan)
            task = db.scalar(select(Task).where(Task.plan_id == plan.plan_id))
            assert task is not None
            decision_event_service.mark_applied(db, decision_key)
            return task
        item_id = action.source_artifact_ids[0]
        try:
            task = leader_service.create_engineering_delivery_task(
                db,
                user,
                project_id,
                run_id,
                item_id,
                allow_direct=True,
            )
            decision_event_service.mark_applied(db, decision_key)
            return task
        except (ConflictException, BusinessException) as exc:
            project_message_service.create_assistant_project_message(
                db,
                user,
                project_id,
                f"暂时无法启动实现修复：{exc}",
                client_message_id=f"leader:repair_blocked:msg:{message_id}",
            )
            decision_event_service.mark_applied(db, decision_key)
            return None

    if action.action == NextActionKind.DISPATCH_ARCHITECT:
        if action.plan is not None:
            plan = plan_service.save_plan(db, user, project_id, run_id, action.plan)
            task = db.scalar(select(Task).where(Task.plan_id == plan.plan_id))
            assert task is not None
            decision_event_service.mark_applied(db, decision_key)
            return task
        item_id = action.source_artifact_ids[0]
        task = leader_service.create_architecture_task(db, user, project_id, run_id, item_id)
        decision_event_service.mark_applied(db, decision_key)
        return task

    if action.action == NextActionKind.NEEDS_LEADER:
        raise BusinessException("NEEDS_LEADER 不能直接执行，必须先完成 Leader 决策")

    raise BusinessException(f"不支持的调度动作：{action.action}")
