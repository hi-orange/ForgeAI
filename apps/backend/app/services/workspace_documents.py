"""Read-only document projections of the run's actual artifact lineage."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.configuration_item import ConfigurationItem
from app.models.plan import Plan
from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.models.task_result import TaskResult
from app.schemas.app_spec import AppSpec
from app.schemas.requirements import RequirementsStatus
from app.schemas.system_design import SystemDesign
from app.services.engineering.claim import read_frozen_input_snapshot
from app.services.task_execution import latest_execution

_DOCUMENTS = {
    "app_spec": ("forgeai/REQUIREMENTS.md", "产品需求"),
    "system_design": ("forgeai/ARCHITECTURE.md", "架构设计"),
}
_LABELS = {
    "goal": "产品目标",
    "target_users": "目标用户",
    "features": "功能范围",
    "data_requirements": "数据需求",
    "interface_requirements": "界面需求",
    "constraints": "约束",
    "acceptance_criteria": "验收标准",
    "open_questions": "待确认问题",
    "architecture_overview": "架构概览",
    "architecture_notes": "设计决策",
    "modules": "模块划分",
    "interfaces": "接口契约",
    "data_structures": "数据结构",
    "technology_choices": "技术选型",
    "implementation_dependencies": "实现依赖",
    "optional_capabilities": "设计选用的通用能力",
    "capability_id": "能力",
    "requirement_ids": "关联需求",
    "responsibility": "职责",
    "dependencies": "依赖",
    "purpose": "用途",
    "kind": "类型",
    "method": "方法",
    "path": "路径",
    "inputs": "输入",
    "outputs": "输出",
    "fields": "字段",
    "relationships": "关联",
    "area": "领域",
    "choice": "选择",
    "rationale": "理由",
    "reason": "原因",
    "source_ids": "关联功能",
    "prerequisite_requirement_id": "前置功能",
    "dependent_requirement_id": "后续功能",
}


def _lines(value: Any) -> list[str]:
    if isinstance(value, list):
        result: list[str] = []
        for index, entry in enumerate(value, 1):
            if isinstance(entry, dict):
                title = entry.get("name") or entry.get("id") or entry.get("area") or str(index)
                result.extend([f"### {title}", ""])
                result.extend(_lines(entry))
            else:
                result.extend(f"- {line}" for line in str(entry).splitlines())
        return [*result, ""] if result else ["暂无。", ""]
    if isinstance(value, dict):
        result = []
        for key, entry in value.items():
            if key in {"id", "name"} or entry is None or entry == []:
                continue
            text = "、".join(str(item) for item in entry) if isinstance(entry, list) else str(entry)
            label = _LABELS.get(key, key)
            result.extend([text if key == "text" else f"{label}：{text}", ""])
        return result
    return [str(value), ""]


def render_document(item: ConfigurationItem, run_id: str) -> str:
    title = _DOCUMENTS[item.semantic_type][1]
    lines = [
        f"# {title}",
        "",
        f"版本：v{item.version} · 只读成果",
        "",
    ]
    if item.state != "usable":
        lines.extend([f"状态：仅供历史查阅（{item.unusable_reason or '已不可用'}）", ""])
    schema = AppSpec if item.semantic_type == "app_spec" else SystemDesign
    # JSONB does not retain key order. Keep the overview before implementation details.
    for key in dict.fromkeys([*schema.model_fields, *item.payload]):
        if key == "optional_capabilities" and not item.payload.get(key):
            continue
        if key in item.payload:
            lines.extend([f"## {_LABELS.get(key, key)}", "", *_lines(item.payload[key])])
    if item.semantic_type == "system_design":
        lines.extend(
            [
                "## 实施指引",
                "",
                "以上模块、接口与实现依赖来自本版本设计。工程师据此安排实施，"
                "当前已接受的工作项、前置依赖与执行状态见 forgeai/PROGRESS.md。",
                "",
                "设计说明不代表功能已经完成；具体文件以实际生成的源码为准。",
                "",
            ]
        )
    lines.extend(
        [
            "## 版本来源",
            "",
            f"构建：{run_id}",
            "",
            f"产物：{item.item_id}",
            "",
            f"来源构建：{item.producer_run_id}",
            "",
        ]
    )
    return "\n".join(lines)


_STATE_LABELS = {
    "not_started": "等待开始",
    "pending": "等待整理需求",
    "running": "正在整理需求",
    "needs_user_input": "等待补充需求",
    "awaiting_approval": "等待确认需求",
    "ready_for_delivery": "需求已确认",
    "design_pending": "等待系统设计",
    "design_running": "正在设计应用",
    "engineering_pending": "等待工程实现",
    "engineering_running": "正在实现应用",
    "engineering_generated": "工程实现已生成，等待独立验证",
    "quality_pending": "等待独立验证",
    "quality_running": "正在独立验证",
    "completed": "独立验证已通过",
    "quality_failed": "独立验证未通过",
    "quality_challenge": "验收存在待确认问题",
    "retry_available": "构建已暂停，可继续处理",
    "stopped": "构建已停止",
}
_TASK_LABELS = {
    "pending": "等待执行",
    "running": "执行中",
    "succeeded": "任务已完成",
    "failed": "执行失败",
    "cancelled": "已停止",
}


def _engineering_snapshot(
    db: Session, project_id: int, status: RequirementsStatus, tasks: list[Task]
) -> tuple[TaskExecution | None, dict[str, Any]]:
    task = next((task for task in reversed(tasks) if task.recipient == "Code Engineer"), None)
    if status.code_item_id:
        # During QA, recover the plan that actually produced this code, not a newer attempt.
        task = db.scalar(
            select(Task)
            .join(Plan, Task.plan_id == Plan.plan_id)
            .join(TaskResult, TaskResult.task_id == Task.task_id)
            .where(
                Plan.project_id == project_id,
                Plan.build_run_id == status.run_id,
                Task.recipient == "Code Engineer",
                TaskResult.configuration_item_id == status.code_item_id,
            )
        )
    if task is None:
        return None, {}
    execution = (
        db.scalar(
            select(TaskExecution).where(
                TaskExecution.execution_id == status.execution_id,
                TaskExecution.task_id == task.task_id,
            )
        )
        if status.execution_id and status.task_id == task.task_id
        else latest_execution(db, task.task_id)
    )
    snapshot = read_frozen_input_snapshot(execution) if execution else None
    if not snapshot or (
        snapshot.get("project_id"),
        snapshot.get("run_id"),
        snapshot.get("task_id"),
    ) != (project_id, status.run_id, task.task_id):
        return None, {}
    return execution, snapshot


def _progress_document(
    status: RequirementsStatus,
    plan: Plan | None,
    tasks: list[Task],
    execution: TaskExecution | None,
    snapshot: dict[str, Any],
) -> str:
    lines = [
        "# 当前进度",
        "",
        "根据本轮已保存的任务与执行记录更新 · 只读",
        "",
        f"当前状态：{_STATE_LABELS[status.state]}",
        "",
    ]
    if status.error:
        lines.extend([f"当前问题：{status.error}", ""])
    if tasks:
        lines.extend(["## 当前阶段任务", ""])
        for task in tasks:
            label = _TASK_LABELS.get(task.status, task.status)
            if task.status == "running" and status.state in {"retry_available", "stopped"}:
                label = "进度已保存，当前未执行"
            lines.extend([f"- {task.title}：{label}"])
        lines.append("")
    checkpoint = snapshot.get("checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    items = checkpoint.get("work_items")
    accepted = not checkpoint.get("needs_delivery_plan") and (
        checkpoint.get("delivery_plan_version") == 1
        or isinstance(snapshot.get("quality_report"), dict)
    )
    lines.extend(["## 实施计划", ""])
    if accepted and isinstance(items, list) and items:
        lines.extend(
            [
                "以下工作项来自当前保存的工程计划，按前置依赖推进；展示顺序不代表固定执行顺序。",
                "完成标记仅表示工程检查通过，独立验收以当前状态为准。",
                "",
            ]
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            completed = item.get("status") == "completed"
            label = "工程检查已通过" if completed else "待实施"
            if not completed and item_id == checkpoint.get("current_work_item_id"):
                label = "当前工作项" if status.state == "engineering_running" else "尚未完成"
            lines.extend(
                [
                    f"### {item.get('title') or item_id}",
                    "",
                    f"工作项：{item_id}",
                    "",
                    f"状态：{label}",
                    "",
                ]
            )
            for key, title in (
                ("requirement_ids", "关联需求"),
                ("depends_on", "前置工作项"),
                ("deliverables", "交付内容"),
                ("acceptance", "检查目标"),
            ):
                values = item.get(key)
                if isinstance(values, list) and values:
                    lines.extend([f"{title}：", "", *[f"- {value}" for value in values], ""])
            if item.get("rationale"):
                lines.extend([f"安排理由：{item['rationale']}", ""])
    else:
        lines.extend(
            ["尚无本轮已接受的工程实施计划。需求与设计文档中的内容仍是目标，不表示已实现。", ""]
        )
    lines.extend(["## 版本来源", "", f"构建：{status.run_id}", ""])
    if plan:
        lines.extend([f"阶段计划：{plan.plan_id} · v{plan.version}", ""])
    if execution:
        lines.extend(
            [
                f"工程任务：{execution.task_id}",
                "",
                f"工程执行：{execution.execution_id} · 第 {execution.attempt} 次",
                "",
            ]
        )
    return "\n".join(lines)


def run_documents(db: Session, project_id: int, status: RequirementsStatus) -> dict[str, bytes]:
    """Follow pinned inputs/results, never the project's newest artifact of each type."""
    if not status.run_id:
        return {}
    plan_query = select(Plan).where(
        Plan.project_id == project_id, Plan.build_run_id == status.run_id
    )
    if status.plan_id:
        plan_query = plan_query.where(Plan.plan_id == status.plan_id)
    plan = db.scalar(plan_query.order_by(Plan.version.desc()).limit(1))
    tasks = (
        list(db.scalars(select(Task).where(Task.plan_id == plan.plan_id).order_by(Task.position)))
        if plan
        else []
    )
    execution, snapshot = _engineering_snapshot(db, project_id, status, tasks)
    roots: list[str] = []
    if status.code_item_id:
        roots.append(status.code_item_id)
    else:
        # Frozen inputs outrank task definition context and earlier task results.
        roots.extend(
            str(snapshot[key])
            for key in ("design_item_id", "approved_item_id")
            if snapshot.get(key)
        )
        if tasks:
            for task in reversed(tasks):
                result = db.get(TaskResult, task.task_id)
                if result is not None:
                    roots.append(result.configuration_item_id)
                roots.extend(task.input_configuration_item_ids)
        if not roots and status.result:
            roots.append(status.result.configuration_item_id)
    documents: dict[str, bytes] = {}
    seen: set[str] = set()
    while roots and len(seen) < 100:
        item_id = roots.pop(0)
        if item_id in seen:
            continue
        seen.add(item_id)
        item = db.scalar(
            select(ConfigurationItem).where(
                ConfigurationItem.project_id == project_id,
                ConfigurationItem.item_id == item_id,
            )
        )
        if item is None or item.semantic_type not in {"app_spec", "system_design", "code"}:
            continue
        if item.semantic_type in _DOCUMENTS:
            path = _DOCUMENTS[item.semantic_type][0]
            if path not in documents:
                documents[path] = render_document(item, status.run_id).encode("utf-8")
        # An app_spec's own upstream is historical context, not this run's design.
        if item.semantic_type != "app_spec":
            roots.extend(item.upstream_item_ids)
    if plan is not None or documents:
        documents["forgeai/PROGRESS.md"] = _progress_document(
            status, plan, tasks, execution, snapshot
        ).encode("utf-8")
    return documents
