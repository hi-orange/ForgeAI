from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents import leader as leader_agent
from app.agents.prompts.leader import (
    ARCHITECTURE_TASK_INSTRUCTIONS,
    CLARIFICATION_TASK_INSTRUCTIONS,
    ENGINEERING_DELIVERY_TASK_INSTRUCTIONS,
    INITIAL_REQUIREMENTS_TASK_INSTRUCTIONS,
    PRODUCT_REVISION_TASK_INSTRUCTIONS,
    QUALITY_VALIDATION_TASK_INSTRUCTIONS,
)
from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.generation.workspace import prepare_engineering_workspace
from app.models.build_run import BuildRun, BuildRunStage, BuildRunStatus
from app.models.configuration_item import ConfigurationItem, ConfigurationItemType
from app.models.plan import Plan
from app.models.project import Project, ProjectStatus
from app.models.project_message import ProjectMessage, ProjectMessageSender
from app.models.project_message_classification import ProjectMessageCategory
from app.models.requirement_clarification import RequirementClarification
from app.models.run_revision import RunRevision
from app.models.task import Task, TaskRecipient
from app.models.task_result import TaskResult
from app.models.user import User
from app.schemas.leader import (
    LeaderContext,
    LeaderMessageSnapshot,
    LeaderOutcome,
    LeaderPlanSnapshot,
    LeaderTaskSnapshot,
    NextActionKind,
)
from app.schemas.plan import PlanCreate
from app.schemas.project_message import ProjectMessageCreate
from app.schemas.task import TaskCreate
from app.schemas.test_report import QualityConclusion, TestReport
from app.services import build_run as build_run_service
from app.services import leader_next_action as leader_next_action_service
from app.services import plan as plan_service
from app.services import project as project_service
from app.services.acceptance_testing import get_or_create_acceptance_test_plan
from app.services.app_spec import read_app_spec
from app.services.engineering import (
    APPROVAL_VERSION,
    ARCHITECTURE_TASK_KEY,
    ENGINEERING_TASK_KEY,
    QUALITY_TASK_KEY,
    find_architecture_task,
    find_claimed_engineering_task,
    find_pending_engineering_task,
    load_approved_app_spec,
    load_engineering_source,
    prepare_requirements_followup,
)
from app.services.project_message import stage_user_project_message
from app.services.project_message_classification import (
    get_stored_classification,
    require_project_message,
)
from app.services.task_execution import lock_run
from app.services.test_engineer import load_test_inputs


class DeliveryPath(StrEnum):
    DIRECT = "direct"
    DESIGNED = "designed"


MAX_QUALITY_CYCLES = 3


def build_leader_context(
    db: Session,
    *,
    project_id: int,
    run_id: str,
    target_message_id: int,
) -> LeaderContext:
    project = db.get(Project, project_id)
    run = db.scalar(
        select(BuildRun).where(BuildRun.project_id == project_id, BuildRun.run_id == run_id)
    )
    target = db.get(ProjectMessage, target_message_id)
    if project is None or run is None or target is None:
        raise NotFoundException("Leader 缺少项目、构建或目标消息上下文")
    recent_rows = list(
        db.scalars(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.sequence < target.sequence,
            )
            .order_by(ProjectMessage.sequence.desc())
            .limit(20)
        ).all()
    )
    recent_rows.reverse()
    plan_rows = list(
        db.scalars(
            select(Plan)
            .where(Plan.project_id == project_id, Plan.build_run_id == run_id)
            .order_by(Plan.version.desc())
            .limit(20)
        ).all()
    )
    plan_rows.reverse()
    plan_snapshots: list[LeaderPlanSnapshot] = []
    for plan in plan_rows:
        task_rows = list(
            db.scalars(
                select(Task).where(Task.plan_id == plan.plan_id).order_by(Task.position)
            ).all()
        )
        task_snapshots: list[LeaderTaskSnapshot] = []
        for task in task_rows:
            saved = db.get(TaskResult, task.task_id)
            item = (
                db.scalar(
                    select(ConfigurationItem).where(
                        ConfigurationItem.item_id == saved.configuration_item_id
                    )
                )
                if saved
                else None
            )
            task_snapshots.append(
                LeaderTaskSnapshot.model_validate(
                    {
                        "task_id": task.task_id,
                        "task_key": task.task_key,
                        "recipient": task.recipient,
                        "title": task.title,
                        "status": task.status,
                        "depends_on_task_ids": list(task.depends_on_task_ids),
                        "result": (
                            {
                                "configuration_item_id": item.item_id,
                                "semantic_type": item.semantic_type,
                                "state": item.state,
                                "payload": item.payload,
                            }
                            if item is not None
                            else None
                        ),
                    }
                )
            )
        plan_snapshots.append(
            LeaderPlanSnapshot.model_validate(
                {
                    "plan_id": plan.plan_id,
                    "version": plan.version,
                    "status": plan.status,
                    "tasks": task_snapshots,
                }
            )
        )
    return LeaderContext(
        project_id=project.id,
        project_name=project.name,
        project_status=project.status,
        run_id=run.run_id,
        run_status=run.status,
        target_message=LeaderMessageSnapshot.model_validate(
            {
                "id": target.id,
                "sequence": target.sequence,
                "sender": target.sender,
                "content": target.content,
            }
        ),
        recent_messages=[
            LeaderMessageSnapshot.model_validate(
                {
                    "id": message.id,
                    "sequence": message.sequence,
                    "sender": message.sender,
                    "content": message.content,
                }
            )
            for message in recent_rows
        ],
        plans=plan_snapshots,
    )


def _delivery_path_from_outcome(
    outcome: LeaderOutcome,
    *,
    approved_item_id: str,
    cause_message_id: int,
) -> DeliveryPath:
    if outcome.action != "dispatch" or outcome.plan is None:
        raise BusinessException("Leader 必须为已批准需求选择下一个交付角色")
    if outcome.intent.category != ProjectMessageCategory.PRODUCT_CHANGE:
        raise BusinessException("Leader 对已批准需求的意图判断不一致")
    if len(outcome.plan.tasks) != 1 or len(outcome.dispatched_task_keys) != 1:
        raise BusinessException("Leader 本轮只能分派一个明确的下游角色")
    task = outcome.plan.tasks[0]
    if (
        outcome.plan.cause_message_id != cause_message_id
        or outcome.dispatched_task_keys != [task.task_key]
        or task.input_configuration_item_ids != [approved_item_id]
        or task.depends_on_task_keys
    ):
        raise BusinessException("Leader 分派没有引用本轮准确的已批准需求")
    if (
        task.recipient == TaskRecipient.ARCHITECT
        and task.expected_output_type == ConfigurationItemType.SYSTEM_DESIGN
    ):
        return DeliveryPath.DESIGNED
    if (
        task.recipient == TaskRecipient.CODE_ENGINEER
        and task.expected_output_type == ConfigurationItemType.CODE
    ):
        return DeliveryPath.DIRECT
    raise BusinessException("Leader 只能把已批准需求交给 Architect 或 Code Engineer")


def create_architecture_task(
    db: Session, user: User, project_id: int, run_id: str, item_id: str
) -> Task:
    """Create the Architect assignment for one exact approved app_spec."""

    try:
        run = lock_run(db, user, project_id, run_id)
        if run.status != "running" or run.stage != "pm" or run.active_slot != 1:
            raise ConflictException("当前构建不能进行需求到架构设计的交接")
        _, source_task, source_plan, approved_spec = load_approved_app_spec(
            db, project_id, run_id, item_id, lock=True
        )
        source_result = db.get(TaskResult, source_task.task_id)
        if source_result is None or source_result.prompt_version != APPROVAL_VERSION:
            raise ConflictException("请先勾选并批准需求计划")
        existing = find_architecture_task(db, source_plan, item_id, lock=True)
        if existing is not None:
            architecture_plan = db.scalar(
                select(Plan).where(Plan.plan_id == existing.plan_id).with_for_update()
            )
            assert architecture_plan is not None
            blocking = db.scalar(
                select(Plan.plan_id)
                .where(
                    Plan.project_id == project_id,
                    Plan.build_run_id == run_id,
                    Plan.version > architecture_plan.version,
                    Plan.status.in_(("pending", "running", "succeeded")),
                )
                .limit(1)
            )
            if blocking is not None:
                raise ConflictException("需求已进入其他后续计划，请刷新进度")
            db.commit()
            db.refresh(existing)
            task = existing
        else:
            latest_version = db.scalar(
                select(Plan.version)
                .where(Plan.project_id == project_id, Plan.build_run_id == run_id)
                .order_by(Plan.version.desc())
                .limit(1)
                .with_for_update()
            )
            if latest_version != source_plan.version:
                raise ConflictException("需求已进入其他后续计划，请刷新进度")
            plan = plan_service.stage_plan(
                db,
                user,
                project_id,
                run_id,
                PlanCreate(
                    version=source_plan.version + 1,
                    cause_message_id=source_plan.cause_message_id,
                    tasks=[
                        TaskCreate(
                            task_key=ARCHITECTURE_TASK_KEY,
                            recipient=TaskRecipient.ARCHITECT,
                            title="根据已批准需求设计系统",
                            instructions=ARCHITECTURE_TASK_INSTRUCTIONS,
                            expected_output_type=ConfigurationItemType.SYSTEM_DESIGN,
                            input_configuration_item_ids=[item_id],
                        )
                    ],
                ),
            )
            created_task = db.scalar(select(Task).where(Task.plan_id == plan.plan_id))
            assert created_task is not None
            task = created_task
            db.commit()
            db.refresh(task)
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("架构设计派工保存冲突，请重试") from exc
    except Exception:
        db.rollback()
        raise

    revision = db.scalar(
        select(RunRevision).where(
            RunRevision.project_id == project_id, RunRevision.target_run_id == run_id
        )
    )
    prepare_engineering_workspace(
        project_id,
        run_id,
        task_id=task.task_id,
        approved_item_id=item_id,
        app_spec=approved_spec.model_dump(mode="json"),
        base_run_id=revision.source_run_id if revision else None,
    )
    return task


def create_engineering_delivery_task(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    item_id: str,
    *,
    allow_direct: bool = False,
) -> Task:
    """Legacy low-level task writer used by Leader.

    Direct ``app_spec`` delivery is intentionally gated so callers cannot bypass
    Leader's complexity decision.
    """
    delivery_task: Task | None = None
    approved_payload: dict | None = None
    try:
        run = lock_run(db, user, project_id, run_id)
        source = load_engineering_source(db, project_id, run_id, item_id, lock=True)
        revision = db.scalar(
            select(RunRevision).where(
                RunRevision.project_id == project_id, RunRevision.target_run_id == run_id
            )
        )
        cross_run = source.source_plan.build_run_id != run_id
        if cross_run:
            if revision is None or revision.kind != "implementation_repair":
                raise ConflictException("跨运行工程输入缺少实现修订关系")
            if run.status == "queued":
                build_run_service.stage_running(
                    db,
                    run,
                    stage=BuildRunStage.DEVELOPER,
                    allowed_running_stages={BuildRunStage.DEVELOPER},
                )
                db.flush()
                db.refresh(run)
        if run.status != "running" or run.active_slot != 1:
            raise ConflictException("当前构建不能进行需求到工程交付的交接")
        acceptance_plan = get_or_create_acceptance_test_plan(
            db,
            project_id=project_id,
            run_id=run_id,
            source=source,
        )
        direct = source.system_design is None
        if direct and not allow_direct:
            raise ConflictException("只有 Leader 可以把简单需求直接交给 Code Engineer")
        allowed_stages = ("pm", "developer") if direct else ("architect", "developer")
        if run.stage not in allowed_stages:
            raise ConflictException("当前构建不能进行需求到工程交付的交接")
        source_plan = source.source_plan
        approved_payload = source.app_spec.model_dump(mode="json")
        claimed = (
            None
            if cross_run
            else find_claimed_engineering_task(
                db, source_plan, item_id, acceptance_plan.item_id, lock=True
            )
        )
        if claimed is not None:
            db.commit()
            db.refresh(claimed)
            delivery_task = claimed
        else:
            expected_stage = "developer" if cross_run else ("pm" if direct else "architect")
            if run.stage != expected_stage:
                raise ConflictException("当前构建不能进行需求到工程交付的交接")
            latest = db.scalar(
                select(Plan)
                .where(Plan.project_id == project_id, Plan.build_run_id == run_id)
                .order_by(Plan.version.desc())
                .limit(1)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not cross_run and (latest is None or latest.version < source_plan.version):
                raise ConflictException("需求已进入其他后续计划，请刷新进度")
            existing = None
            if cross_run:
                for candidate_plan in db.scalars(
                    select(Plan)
                    .where(Plan.project_id == project_id, Plan.build_run_id == run_id)
                    .order_by(Plan.version)
                ).all():
                    candidate_tasks = list(
                        db.scalars(select(Task).where(Task.plan_id == candidate_plan.plan_id)).all()
                    )
                    expected_inputs = [item_id, acceptance_plan.item_id]
                    if (
                        len(candidate_tasks) == 1
                        and candidate_tasks[0].input_configuration_item_ids == expected_inputs
                    ):
                        existing = candidate_tasks[0]
                        break
            else:
                existing = find_pending_engineering_task(
                    db, source_plan, item_id, acceptance_plan.item_id, lock=True
                )
            if existing is not None:
                if existing.task_key == ENGINEERING_TASK_KEY:
                    engineering_plan = db.scalar(
                        select(Plan).where(Plan.plan_id == existing.plan_id).with_for_update()
                    )
                    assert engineering_plan is not None
                    blocking = db.scalar(
                        select(Plan.plan_id)
                        .where(
                            Plan.project_id == project_id,
                            Plan.build_run_id == run_id,
                            Plan.version > engineering_plan.version,
                            Plan.status.in_(("pending", "running", "succeeded")),
                        )
                        .limit(1)
                    )
                    if blocking is not None:
                        raise ConflictException("需求已进入其他后续计划，请刷新进度")
                    db.commit()
                    db.refresh(existing)
                    delivery_task = existing
            if delivery_task is None:
                if existing is None:
                    blocking = db.scalar(
                        select(Plan.plan_id)
                        .where(
                            Plan.project_id == project_id,
                            Plan.build_run_id == run_id,
                            Plan.version > source_plan.version,
                            Plan.status.in_(("pending", "running", "succeeded")),
                        )
                        .limit(1)
                    )
                    if blocking is not None:
                        raise ConflictException("需求已进入其他后续计划，请刷新进度")
                next_version = (
                    db.scalar(
                        select(Plan.version)
                        .where(Plan.project_id == project_id, Plan.build_run_id == run_id)
                        .order_by(Plan.version.desc())
                        .limit(1)
                    )
                    or (0 if cross_run else source_plan.version)
                ) + 1
                plan = plan_service.stage_plan(
                    db,
                    user,
                    project_id,
                    run_id,
                    PlanCreate(
                        version=next_version,
                        cause_message_id=(
                            revision.cause_message_id
                            if cross_run and revision
                            else source_plan.cause_message_id
                        ),
                        tasks=[
                            TaskCreate(
                                task_key=ENGINEERING_TASK_KEY,
                                recipient=TaskRecipient.CODE_ENGINEER,
                                title=(
                                    "根据已批准需求直接交付应用代码"
                                    if direct
                                    else "根据系统设计交付应用代码"
                                ),
                                instructions=ENGINEERING_DELIVERY_TASK_INSTRUCTIONS,
                                expected_output_type=ConfigurationItemType.CODE,
                                input_configuration_item_ids=[item_id, acceptance_plan.item_id],
                            )
                        ],
                    ),
                )
                task = db.scalar(select(Task).where(Task.plan_id == plan.plan_id))
                assert task is not None
                db.commit()
                db.refresh(task)
                delivery_task = task
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("工程交付派工保存冲突，请重试") from exc
    except Exception:
        db.rollback()
        raise

    assert delivery_task is not None and approved_payload is not None
    prepare_engineering_workspace(
        project_id,
        run_id,
        task_id=delivery_task.task_id,
        approved_item_id=source.app_spec_item.item_id,
        app_spec=approved_payload,
        base_run_id=revision.source_run_id if revision else None,
    )
    return delivery_task


def create_initial_plan(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    message_id: int,
) -> Plan:
    """为已有 BuildRun 安排首次需求整理；只读取已保存的分类，不执行任务。

    新建仅支持尚无正式成果的 draft 项目。同一运行的 Plan v1 可以重放，
    但不能换一条消息覆盖旧计划，也不负责后续需求变更或修复规划。
    """

    project_service.get_user_project(db, user, project_id)
    message = require_project_message(db, project_id, message_id)
    if message.sender != ProjectMessageSender.USER.value:
        raise BusinessException("只能根据用户消息创建初始计划")
    classification = get_stored_classification(db, message.id)
    if classification is None:
        raise NotFoundException("消息尚未分类")
    if classification.category != ProjectMessageCategory.PRODUCT_CHANGE.value:
        raise BusinessException("初始计划只接受 product_change 类型的用户消息")

    payload = PlanCreate(
        version=1,
        cause_message_id=message.id,
        tasks=[
            TaskCreate(
                task_key="requirements",
                recipient=TaskRecipient.PRODUCT_MANAGER,
                title="整理应用需求",
                instructions=INITIAL_REQUIREMENTS_TASK_INSTRUCTIONS,
                expected_output_type=ConfigurationItemType.APP_SPEC,
            )
        ],
    )
    try:
        # 与 Plan / ConfigurationManager 一样先锁项目，避免检查后被并发发布的成果绕过。
        project = db.scalar(
            select(Project)
            .where(Project.id == project_id, Project.user_id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            raise NotFoundException("项目不存在")
        plans = list(
            db.scalars(
                select(Plan)
                .where(Plan.project_id == project_id, Plan.build_run_id == run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        is_replay = any(plan.version == 1 for plan in plans)
        if not is_replay:
            if plans:
                raise ConflictException("该 BuildRun 已有计划，不能补建初始计划")
            if project.status != ProjectStatus.DRAFT.value:
                raise ConflictException("项目已有可用版本，不能按首次构建创建计划")
            # 包括不可用成果：本入口不猜测应该复用还是重做已有结果。
            existing_item = db.scalar(
                select(ConfigurationItem.item_id)
                .where(ConfigurationItem.project_id == project_id)
                .limit(1)
                .with_for_update()
            )
            if existing_item is not None:
                raise ConflictException("项目已有正式成果，需要另行制定后续计划")

        # 版本冲突、运行资格、整份计划的原子写入由现有保存服务负责。
        plan = plan_service.save_plan(db, user, project_id, run_id, payload)
        if is_replay:
            # save_plan 的历史重放不提交；释放本入口取得的项目锁。
            db.commit()
        return plan
    except Exception:
        db.rollback()
        raise


def create_clarification_plan(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    item_id: str,
    answer: ProjectMessageCreate,
) -> Plan:
    """原子保存补充回答、新计划及关联；一版原需求最多接受一份回答。"""

    answer = ProjectMessageCreate.model_validate(answer.model_dump())
    try:
        run = lock_run(db, user, project_id, run_id)
        if (run.status, run.stage, run.active_slot) != ("running", "pm", 1):
            raise ConflictException("当前构建不能继续补充需求")
        row = db.execute(
            select(ConfigurationItem, Task, Plan)
            .join(TaskResult, TaskResult.configuration_item_id == ConfigurationItem.item_id)
            .join(Task, Task.task_id == TaskResult.task_id)
            .join(Plan, Plan.plan_id == Task.plan_id)
            .where(
                ConfigurationItem.item_id == item_id,
                ConfigurationItem.project_id == project_id,
                ConfigurationItem.producer_run_id == run_id,
                Plan.project_id == project_id,
                Plan.build_run_id == run_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            raise NotFoundException("待补充的需求不存在或不属于当前构建")
        item, old_task, old_plan = row
        if (
            (item.semantic_type, item.state) != ("app_spec", "usable")
            or old_task.status != "succeeded"
            or old_plan.status != "succeeded"
        ):
            raise ConflictException("只有已完成的可用需求能够接受补充回答")
        read_app_spec(item)
        source_result = db.get(TaskResult, old_task.task_id)
        if source_result is not None and source_result.prompt_version == APPROVAL_VERSION:
            raise ConflictException("需求已批准，请刷新进度")
        clarification = db.scalar(
            select(RequirementClarification)
            .where(RequirementClarification.configuration_item_id == item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if clarification is not None and clarification.answer_message_id is not None:
            previous_answer = db.scalar(
                select(ProjectMessage)
                .where(ProjectMessage.id == clarification.answer_message_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if previous_answer is None or (
                previous_answer.client_message_id != answer.client_message_id
                or previous_answer.content != answer.content
            ):
                raise ConflictException("这版需求已收到另一份回答，请刷新后继续")
            replay = db.scalar(
                select(Plan)
                .where(Plan.plan_id == clarification.followup_plan_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if replay is None:
                raise ConflictException("补充计划关联异常")
            db.commit()
            return replay
        next_version = prepare_requirements_followup(db, old_plan, item_id)
        message = stage_user_project_message(db, user, project_id, answer)
        original = db.get(ProjectMessage, old_plan.cause_message_id)
        if original is None or message.sequence <= original.sequence:
            raise BusinessException("补充回答必须晚于原需求消息")
        plan = plan_service.stage_plan(
            db,
            user,
            project_id,
            run_id,
            PlanCreate(
                version=next_version,
                cause_message_id=message.id,
                tasks=[
                    TaskCreate(
                        task_key="requirements",
                        recipient=TaskRecipient.PRODUCT_MANAGER,
                        title="根据用户补充完善需求",
                        instructions=CLARIFICATION_TASK_INSTRUCTIONS,
                        expected_output_type=ConfigurationItemType.APP_SPEC,
                        input_configuration_item_ids=[item_id],
                    )
                ],
            ),
        )
        # 兼容迁移前已保存且有待确认问题的 app_spec，首次回答时建立关联。
        if clarification is None:
            clarification = RequirementClarification(
                configuration_item_id=item_id, task_id=old_task.task_id
            )
            db.add(clarification)
        clarification.answer_message_id = message.id
        clarification.followup_plan_id = plan.plan_id
        db.commit()
        db.refresh(plan)
        return plan
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("补充回答保存冲突，请使用相同请求重试") from exc
    except Exception:
        db.rollback()
        raise


def create_product_revision_plan(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    approved_item_id: str,
    report_item_id: str,
    revision: ProjectMessageCreate,
) -> Plan:
    """Create a new PM revision from one exact blocked report without mutating frozen inputs."""

    revision = ProjectMessageCreate.model_validate(revision.model_dump())
    try:
        run = lock_run(db, user, project_id, run_id)
        if (run.status, run.stage, run.active_slot) != ("running", "pm", 1):
            raise ConflictException("当前构建不能修订产品意图")
        approved_item, approved_task, _, _ = load_approved_app_spec(
            db, project_id, run_id, approved_item_id, lock=True
        )
        approved_result = db.get(TaskResult, approved_task.task_id)
        if approved_result is None or approved_result.prompt_version != APPROVAL_VERSION:
            raise ConflictException("产品修订必须基于准确的已批准需求")

        report_row = db.execute(
            select(ConfigurationItem, Task, Plan)
            .join(TaskResult, TaskResult.configuration_item_id == ConfigurationItem.item_id)
            .join(Task, Task.task_id == TaskResult.task_id)
            .join(Plan, Plan.plan_id == Task.plan_id)
            .where(
                ConfigurationItem.item_id == report_item_id,
                ConfigurationItem.project_id == project_id,
                ConfigurationItem.producer_run_id == run_id,
                Plan.project_id == project_id,
                Plan.build_run_id == run_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if report_row is None:
            raise NotFoundException("测试质疑报告不存在或不属于当前构建")
        report_item, report_task, report_plan = report_row
        if (
            (report_item.semantic_type, report_item.state) != ("test_report", "usable")
            or report_task.task_key != QUALITY_TASK_KEY
            or report_task.recipient != TaskRecipient.TEST_ENGINEER.value
            or report_task.status != "succeeded"
            or report_plan.status != "succeeded"
            or len(report_item.upstream_item_ids) not in {1, 2}
        ):
            raise ConflictException("只有已完成的测试质疑报告可以触发产品修订")
        report = TestReport.model_validate(report_item.payload)
        if report.quality_conclusion != QualityConclusion.BLOCKED or not report.test_challenges:
            raise ConflictException("该质量报告没有待仲裁的测试质疑")

        inputs = load_test_inputs(
            db, project_id, run_id, report_item.upstream_item_ids[0], lock=True
        )
        source = load_engineering_source(
            db, project_id, run_id, inputs.code_item.upstream_item_ids[0], lock=True
        )
        if source.app_spec_item.item_id != approved_item.item_id:
            raise ConflictException("测试质疑与待修订的已批准需求身份不一致")
        latest = db.scalar(
            select(Plan)
            .where(Plan.project_id == project_id, Plan.build_run_id == run_id)
            .order_by(Plan.version.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if latest is None or latest.plan_id != report_plan.plan_id:
            raise ConflictException("测试质疑已经进入其他后续计划")

        clarification = db.scalar(
            select(RequirementClarification)
            .where(RequirementClarification.configuration_item_id == approved_item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if clarification is not None and clarification.answer_message_id is not None:
            previous = db.get(ProjectMessage, clarification.answer_message_id)
            if previous is None or (
                previous.client_message_id != revision.client_message_id
                or previous.content != revision.content
            ):
                raise ConflictException("这版测试质疑已经收到另一项仲裁决定")
            replay = db.get(Plan, clarification.followup_plan_id)
            if replay is None:
                raise ConflictException("产品修订计划关联异常")
            db.commit()
            return replay

        message = stage_user_project_message(db, user, project_id, revision)
        plan = plan_service.stage_plan(
            db,
            user,
            project_id,
            run_id,
            PlanCreate(
                version=latest.version + 1,
                cause_message_id=message.id,
                tasks=[
                    TaskCreate(
                        task_key="requirements",
                        recipient=TaskRecipient.PRODUCT_MANAGER,
                        title="根据测试质疑修订产品意图",
                        instructions=PRODUCT_REVISION_TASK_INSTRUCTIONS,
                        expected_output_type=ConfigurationItemType.APP_SPEC,
                        input_configuration_item_ids=[approved_item_id],
                    )
                ],
            ),
        )
        if clarification is None:
            clarification = RequirementClarification(
                configuration_item_id=approved_item_id,
                task_id=approved_task.task_id,
            )
            db.add(clarification)
        clarification.answer_message_id = message.id
        clarification.followup_plan_id = plan.plan_id
        db.commit()
        db.refresh(plan)
        return plan
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("产品修订计划保存冲突，请使用相同请求重试") from exc
    except Exception:
        db.rollback()
        raise


def dispatch_approved_requirements(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    approved_item_id: str,
) -> Task:
    """Let Leader choose, then persist one validated next-role assignment."""

    project_service.get_user_project(db, user, project_id)
    _, _, source_plan, _ = load_approved_app_spec(db, project_id, run_id, approved_item_id)
    context = build_leader_context(
        db,
        project_id=project_id,
        run_id=run_id,
        target_message_id=source_plan.cause_message_id,
    )
    outcome = leader_agent.lead_project_turn(
        context,
        instruction=(
            "本轮只决定已批准 app_spec 的下一个角色。创建且分派一个任务。若命中 "
            "external_integration、auth_or_permission、concurrency、persisted_contract_migration、"
            "breaking_public_contract 任一标志，或新增超过 1 个相互关联业务实体，交给 Architect；"
            "仅当这些标志全为 false、最多影响 2 个既有业务模块且契约已明确时，才交给 "
            "Code Engineer。任务 instructions 首句必须记录命中的 reason code。任务只引用准确成果 "
            f"{approved_item_id}，不得安排 Product Manager、Test Engineer 或未来任务。"
        ),
        max_turns=leader_agent.LEADER_SIMPLE_DISPATCH_TURNS,
    )
    path = _delivery_path_from_outcome(
        outcome,
        approved_item_id=approved_item_id,
        cause_message_id=source_plan.cause_message_id,
    )
    if path == DeliveryPath.DIRECT:
        return create_engineering_delivery_task(
            db,
            user,
            project_id,
            run_id,
            approved_item_id,
            allow_direct=True,
        )
    return create_architecture_task(db, user, project_id, run_id, approved_item_id)


def dispatch_completed_design(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    design_item_id: str,
) -> Task:
    """Deterministic handoff: completed system_design has only one legal next role."""

    return create_engineering_delivery_task(db, user, project_id, run_id, design_item_id)


def dispatch_completed_code(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    code_item_id: str,
) -> Task:
    """Deterministic handoff: acceptance is the mandatory release gate after code."""

    try:
        run = lock_run(db, user, project_id, run_id)
        if run.status != "running" or run.stage != "developer" or run.active_slot != 1:
            raise ConflictException("当前构建不能进行代码到质量验证的交接")
        inputs = load_test_inputs(db, project_id, run_id, code_item_id, lock=True)
        latest = db.scalar(
            select(Plan)
            .where(Plan.project_id == project_id, Plan.build_run_id == run_id)
            .order_by(Plan.version.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if latest is None:
            raise ConflictException("当前构建没有可继续的计划")
        if latest.version > inputs.code_plan.version:
            tasks = list(db.scalars(select(Task).where(Task.plan_id == latest.plan_id)).all())
            if len(tasks) == 1:
                replay = tasks[0]
                if (
                    replay.task_key == QUALITY_TASK_KEY
                    and replay.recipient == TaskRecipient.TEST_ENGINEER.value
                    and replay.expected_output_type == ConfigurationItemType.TEST_REPORT.value
                    and replay.input_configuration_item_ids == [code_item_id]
                    and not replay.depends_on_task_ids
                    and replay.status
                    in {
                        "pending",
                        "running",
                        "succeeded",
                    }
                ):
                    db.commit()
                    db.refresh(replay)
                    return replay
            raise ConflictException("代码成果已进入其他后续计划，请刷新进度")
        if latest.plan_id != inputs.code_plan.plan_id:
            raise ConflictException("代码成果不是当前最新计划的结果")
        plan = plan_service.stage_plan(
            db,
            user,
            project_id,
            run_id,
            PlanCreate(
                version=latest.version + 1,
                cause_message_id=latest.cause_message_id,
                tasks=[
                    TaskCreate(
                        task_key=QUALITY_TASK_KEY,
                        recipient=TaskRecipient.TEST_ENGINEER,
                        title="独立验证应用代码",
                        instructions=QUALITY_VALIDATION_TASK_INSTRUCTIONS,
                        expected_output_type=ConfigurationItemType.TEST_REPORT,
                        input_configuration_item_ids=[code_item_id],
                    )
                ],
            ),
        )
        task = db.scalar(select(Task).where(Task.plan_id == plan.plan_id))
        assert task is not None
        db.commit()
        db.refresh(task)
        return task
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("质量验证派工保存冲突，请重试") from exc
    except Exception:
        db.rollback()
        raise


def retry_blocked_quality_validation(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    report_item_id: str,
) -> Task:
    """Retry QA for unchanged code after a recoverable platform-evidence block.

    A blocked report without a test challenge is not a code defect. Re-running
    Code Engineer cannot repair missing or temporarily unavailable platform
    evidence, so recovery creates another read-only Test Engineer assignment for
    the exact same code identity and remains bounded by MAX_QUALITY_CYCLES.
    """

    try:
        run = lock_run(db, user, project_id, run_id)
        if (run.status, run.stage, run.active_slot) != ("failed", "qa", None):
            raise ConflictException("当前构建不处于可恢复的质量阻塞状态")
        row = db.execute(
            select(ConfigurationItem, Task, Plan)
            .join(TaskResult, TaskResult.configuration_item_id == ConfigurationItem.item_id)
            .join(Task, Task.task_id == TaskResult.task_id)
            .join(Plan, Plan.plan_id == Task.plan_id)
            .where(
                ConfigurationItem.item_id == report_item_id,
                ConfigurationItem.project_id == project_id,
                ConfigurationItem.producer_run_id == run_id,
                Plan.project_id == project_id,
                Plan.build_run_id == run_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            raise NotFoundException("质量报告不存在或不属于当前构建")
        report_item, quality_task, quality_plan = row
        report = TestReport.model_validate(report_item.payload)
        if (
            report_item.semantic_type != ConfigurationItemType.TEST_REPORT.value
            or report_item.state != "usable"
            or quality_task.task_key != QUALITY_TASK_KEY
            or quality_task.recipient != TaskRecipient.TEST_ENGINEER.value
            or quality_task.status != "succeeded"
            or quality_plan.status != "succeeded"
            or report.quality_conclusion != QualityConclusion.BLOCKED
            or report.test_challenges
        ):
            raise ConflictException("只有无验收冲突的基础设施阻塞可以重试独立验证")
        inputs = load_test_inputs(
            db,
            project_id,
            run_id,
            report.code_item_id,
            lock=True,
        )
        if inputs.acceptance_test_plan_item is not None and (
            report.test_plan_item_id != inputs.acceptance_test_plan_item.item_id
            or report.test_hash != inputs.acceptance_test_plan_item.content_hash
        ):
            raise ConflictException("质量报告与冻结验收测试计划身份不一致")
        latest = db.scalar(
            select(Plan)
            .where(Plan.project_id == project_id, Plan.build_run_id == run_id)
            .order_by(Plan.version.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if latest is None or latest.plan_id != quality_plan.plan_id:
            raise ConflictException("质量报告已进入其他后续计划")
        quality_cycles = db.scalar(
            select(func.count())
            .select_from(Task)
            .join(Plan, Task.plan_id == Plan.plan_id)
            .where(
                Plan.project_id == project_id,
                Plan.build_run_id == run_id,
                Task.task_key == QUALITY_TASK_KEY,
                Task.recipient == TaskRecipient.TEST_ENGINEER.value,
            )
        )
        if int(quality_cycles or 0) >= MAX_QUALITY_CYCLES:
            raise ConflictException("独立验收重试轮次已用尽")
        # Plan validation only accepts active BuildRuns. Reactivate the locked run
        # inside this transaction before staging the retry plan, and flush so the
        # validator's SELECT ... FOR UPDATE observes the new state. Any later
        # failure rolls both the run transition and the plan back together.
        run.status = BuildRunStatus.RUNNING.value
        run.stage = "qa"
        run.active_slot = 1
        run.error = None
        db.flush()
        plan = plan_service.stage_plan(
            db,
            user,
            project_id,
            run_id,
            PlanCreate(
                version=latest.version + 1,
                cause_message_id=latest.cause_message_id,
                tasks=[
                    TaskCreate(
                        task_key=QUALITY_TASK_KEY,
                        recipient=TaskRecipient.TEST_ENGINEER,
                        title="恢复独立验证",
                        instructions=(
                            "对同一准确代码结果重新收集平台验收证据；"
                            "不得把平台验收基础设施问题转交 Code Engineer。"
                        ),
                        expected_output_type=ConfigurationItemType.TEST_REPORT,
                        input_configuration_item_ids=[inputs.code_item.item_id],
                    )
                ],
            ),
        )
        task = db.scalar(select(Task).where(Task.plan_id == plan.plan_id))
        assert task is not None
        db.commit()
        db.refresh(task)
        return task
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("质量验证恢复派工保存冲突，请重试") from exc
    except Exception:
        db.rollback()
        raise


def dispatch_quality_repair(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    report_item_id: str,
    *,
    challenge_resolved: bool = False,
) -> Task | None:
    """Route failed/blocked QA evidence through NextAction failure routing."""

    try:
        run = lock_run(db, user, project_id, run_id)
        if run.status != "running" or run.stage != "qa" or run.active_slot != 1:
            raise ConflictException("当前构建不能从质量验证进入修复")
        row = db.execute(
            select(ConfigurationItem, Task, Plan)
            .join(TaskResult, TaskResult.configuration_item_id == ConfigurationItem.item_id)
            .join(Task, Task.task_id == TaskResult.task_id)
            .join(Plan, Plan.plan_id == Task.plan_id)
            .where(
                ConfigurationItem.item_id == report_item_id,
                ConfigurationItem.project_id == project_id,
                ConfigurationItem.producer_run_id == run_id,
                Plan.project_id == project_id,
                Plan.build_run_id == run_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            raise NotFoundException("质量报告不存在或不属于当前构建")
        report_item, quality_task, quality_plan = row
        if (
            report_item.semantic_type != ConfigurationItemType.TEST_REPORT.value
            or report_item.state != "usable"
            or quality_task.task_key != QUALITY_TASK_KEY
            or quality_task.recipient != TaskRecipient.TEST_ENGINEER.value
            or quality_task.status != "succeeded"
            or quality_plan.status != "succeeded"
            or len(report_item.upstream_item_ids) not in {1, 2}
        ):
            raise ConflictException("只有已完成的独立验收报告可以触发修复")
        report = TestReport.model_validate(report_item.payload)
        if report.quality_conclusion == QualityConclusion.PASSED:
            raise ConflictException("已通过的质量报告不需要修复")
        inputs = load_test_inputs(
            db,
            project_id,
            run_id,
            report_item.upstream_item_ids[0],
            lock=True,
        )
        if report.code_item_id != inputs.code_item.item_id:
            raise ConflictException("质量报告与被验证代码身份不一致")
        if inputs.acceptance_test_plan_item is not None and (
            report.test_plan_item_id != inputs.acceptance_test_plan_item.item_id
            or report.test_hash != inputs.acceptance_test_plan_item.content_hash
        ):
            raise ConflictException("质量报告与冻结验收测试计划身份不一致")
        latest = db.scalar(
            select(Plan)
            .where(Plan.project_id == project_id, Plan.build_run_id == run_id)
            .order_by(Plan.version.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if latest is None or latest.plan_id != quality_plan.plan_id:
            raise ConflictException("质量报告已进入其他后续计划")
        quality_tasks = list(
            db.scalars(
                select(Task)
                .join(Plan, Task.plan_id == Plan.plan_id)
                .where(
                    Plan.project_id == project_id,
                    Plan.build_run_id == run_id,
                    Task.task_key == QUALITY_TASK_KEY,
                    Task.recipient == TaskRecipient.TEST_ENGINEER.value,
                )
            ).all()
        )
        if len(quality_tasks) >= MAX_QUALITY_CYCLES:
            run.status = BuildRunStatus.FAILED.value
            run.active_slot = None
            run.error = (f"独立验收连续 {MAX_QUALITY_CYCLES} 轮未通过：{report.summary}")[:500]
            db.commit()
            return None

        source_item_id = inputs.code_item.upstream_item_ids[0]
        next_action = leader_next_action_service.decide_quality_failure_next_action(
            db,
            user,
            project_id=project_id,
            run_id=run_id,
            report=report,
            approved_item_id=source_item_id,
            code_item_id=inputs.code_item.item_id,
            report_item_id=report_item_id,
            cause_message_id=latest.cause_message_id,
            challenge_resolved=challenge_resolved,
        )
        if next_action.action == NextActionKind.RETRY_INFRASTRUCTURE:
            run.error = (next_action.summary or "验收环境未就绪，请修复基础设施后继续")[:500]
            db.commit()
            return None
        if next_action.action == NextActionKind.AWAIT_USER_CHALLENGE:
            run.error = (next_action.summary or "存在测试质疑，等待用户裁决")[:500]
            db.commit()
            return None
        if next_action.action == NextActionKind.REPLY:
            run.error = (next_action.reply_text or next_action.summary)[:500]
            db.commit()
            return None

        if next_action.plan is not None:
            plan = plan_service.stage_plan(db, user, project_id, run_id, next_action.plan)
            task = db.scalar(select(Task).where(Task.plan_id == plan.plan_id))
            assert task is not None
            run.error = None
            db.commit()
            db.refresh(task)
            return task

        if next_action.action != NextActionKind.DISPATCH_CODE_ENGINEER:
            raise BusinessException(f"质量失败分流得到不支持的动作：{next_action.action}")

        repair_inputs = [source_item_id]
        if inputs.acceptance_test_plan_item is not None:
            repair_inputs.append(inputs.acceptance_test_plan_item.item_id)
        repair_inputs.append(report_item_id)
        plan = plan_service.stage_plan(
            db,
            user,
            project_id,
            run_id,
            PlanCreate(
                version=latest.version + 1,
                cause_message_id=latest.cause_message_id,
                tasks=[
                    TaskCreate(
                        task_key=ENGINEERING_TASK_KEY,
                        recipient=TaskRecipient.CODE_ENGINEER,
                        title=f"根据第 {len(quality_tasks)} 轮独立验收修复代码",
                        instructions=(
                            "只修复输入 test_report 中有证据的缺陷，保留已经通过的功能；"
                            "完成平台检查后重新交给独立 Test Engineer 验收。"
                        ),
                        expected_output_type=ConfigurationItemType.CODE,
                        input_configuration_item_ids=repair_inputs,
                    )
                ],
            ),
        )
        task = db.scalar(select(Task).where(Task.plan_id == plan.plan_id))
        assert task is not None
        run.error = None
        db.commit()
        db.refresh(task)
        return task
    except IntegrityError as exc:
        db.rollback()
        raise ConflictException("质量修复派工保存冲突，请重试") from exc
    except Exception:
        db.rollback()
        raise
