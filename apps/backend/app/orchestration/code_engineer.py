"""Engineering coding loop: frozen inputs → tools → workspace files. Does not publish."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict, cast

from sqlalchemy.orm import Session, sessionmaker

from app.agents.code_engineer import decide_next_action, generate_file_content
from app.core.exceptions import BusinessException, ConflictException
from app.core.settings import settings
from app.generation.delivery import WorkItem, plan_delivery, work_items_to_json
from app.generation.workspace import default_workspace_path, workspace_is_ready
from app.models.user import User
from app.retrieval.code_context import retrieve_code_context as retrieve_workspace_context
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.schemas.system_design import SystemDesign
from app.services.engineering.claim import DEFAULT_CALL_BUDGET, read_frozen_input_snapshot
from app.services.engineering.handoff import load_approved_app_spec
from app.services.task_execution import latest_execution, renew_execution_lease, utc_now
from app.tools import checks as check_tools
from app.tools import files as file_tools
from app.tools.code_engineer import execute_tool_call
from app.tools.paths import safe_path_under_root, sha256_bytes

CHECKPOINT_KIND = "engineering_checkpoint"
CHECKPOINT_SCHEMA_VERSION = 2
MAX_OBSERVATIONS = 20
MAX_DETAILED_EDITOR_OBSERVATIONS = 5
MAX_ACTIVITY = 80
MAX_CONTEXT_FILES = 160
MAX_MEMORY_FACTS = 80

_TOOL_LABELS = {
    "list_files": "List files",
    "read_file": "Read file",
    "search_code": "Search code",
    "retrieve_code_context": "Retrieve context",
    "write_new_code": "Generate file",
    "edit_file_by_replace": "Edit file",
    "record_engineering_memory": "Remember contract",
    "run_check": "Run checks",
    "complete_work_item": "Complete work item",
    "report_blocked": "Report blocked",
}


def _activity_entry(call: ToolCall, result: ToolExecutionResult) -> dict[str, Any]:
    args = call.arguments
    detail = ""
    if not result.ok:
        detail = result.summary
    elif call.name in {"read_file", "edit_file_by_replace", "write_new_code"}:
        detail = str(args.get("path") or "")
    elif call.name == "list_files":
        detail = str(args.get("path") or ".")
    elif call.name == "search_code":
        detail = str(args.get("query") or "")[:80]
    elif call.name == "retrieve_code_context":
        detail = str(args.get("query") or "")[:80]
    elif call.name == "complete_work_item":
        detail = str(args.get("work_item_id") or "")
    else:
        detail = (result.summary or "")[:80]
    return {
        "id": call.id,
        "name": call.name,
        "label": _TOOL_LABELS.get(call.name, call.name),
        "detail": detail[:200],
        "ok": result.ok,
    }


def _progress_summary(content: str | None, call: ToolCall, work_item: WorkItem) -> str:
    """Expose a concise next action, never the model's private reasoning trace."""
    if content and content.strip():
        first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
        if first_line and any("\u4e00" <= char <= "\u9fff" for char in first_line):
            return first_line[:200]
    args = call.arguments
    if call.name == "list_files":
        return f"先查看 {str(args.get('path') or '项目根目录')} 的文件结构。"[:200]
    if call.name == "read_file":
        return f"读取 {str(args.get('path') or '现有文件')}，确认实现后再决定修改。"[:200]
    if call.name == "search_code":
        return f"搜索“{str(args.get('query') or '')[:80]}”，定位关联实现。"
    if call.name == "retrieve_code_context":
        return f"RAG 检索“{str(args.get('query') or '')[:80]}”，补充候选上下文。"
    if call.name == "edit_file_by_replace":
        return f"局部修改 {str(args.get('path') or '目标文件')}，推进“{work_item.title}”。"[:200]
    if call.name == "write_new_code":
        target = str(args.get("path") or "目标文件")
        return f"生成完整文件 {target}，推进“{work_item.title}”。"[:200]
    if call.name == "record_engineering_memory":
        return f"记录已确认的工程契约：{str(args.get('subject') or '')[:100]}。"
    if call.name == "complete_work_item":
        return f"“{work_item.title}”已完成，记录结果并继续下一项。"[:200]
    if call.name == "run_check":
        return f"运行 {str(args.get('check_id') or 'all')} 检查，验证最新源码可运行。"[:200]
    if call.name == "report_blocked":
        return "当前实现遇到阻断，保存现场并等待处理。"
    return f"继续处理“{work_item.title}”。"[:200]


def _call_budget(snapshot: dict[str, Any], name: str) -> int:
    configured = snapshot.get("call_budget")
    value = configured.get(name) if isinstance(configured, dict) else None
    if isinstance(value, int) and value > 0:
        return value
    return DEFAULT_CALL_BUDGET[name]


def _build_workspace_file_index(root: Path) -> dict[str, Any]:
    """Create one bounded file map; later writes update it incrementally."""
    listing = file_tools.list_files(root, tool_call_id="context_file_index")
    raw_files = listing.data.get("files") if listing.ok else []
    files = raw_files if isinstance(raw_files, list) else []
    file_paths = [
        str(item.get("path"))
        for item in files[:MAX_CONTEXT_FILES]
        if isinstance(item, dict) and item.get("path")
    ]
    return {
        "paths": file_paths,
        "visible_file_count": len(files),
        "truncated": bool(listing.truncated or len(files) > MAX_CONTEXT_FILES),
    }


def _new_long_term_memory() -> dict[str, Any]:
    return {"facts": [], "files": {}}


def _update_file_memory(
    checkpoint: dict[str, Any], result: ToolExecutionResult, work_item_id: str
) -> None:
    if not result.ok or result.name not in {
        "read_file",
        "edit_file_by_replace",
        "write_new_code",
    }:
        return
    path = str(result.data.get("path") or result.arguments.get("path") or "").replace("\\", "/")
    content_hash = result.data.get("content_hash")
    if not path or not isinstance(content_hash, str):
        return
    memory = checkpoint.setdefault("long_term_memory", _new_long_term_memory())
    files = memory.setdefault("files", {})
    if not isinstance(files, dict):
        files = {}
        memory["files"] = files
    files[path] = {
        "content_hash": content_hash,
        "last_action": result.name,
        "line_count": result.data.get("line_count"),
        "work_item_id": work_item_id,
    }
    if len(files) > MAX_CONTEXT_FILES:
        for stale_path in list(files)[: len(files) - MAX_CONTEXT_FILES]:
            files.pop(stale_path, None)


def _has_current_file_evidence(
    observations: list[ToolExecutionResult],
    *,
    path: str,
    content_hash: str,
) -> bool:
    """Require current-turn source evidence; RAG excerpts never authorize writes."""

    normalized_path = path.replace("\\", "/").strip()
    for observation in reversed(observations):
        if not observation.ok or observation.name not in {
            "read_file",
            "edit_file_by_replace",
            "write_new_code",
        }:
            continue
        observed_path = str(
            observation.data.get("path") or observation.arguments.get("path") or ""
        ).replace("\\", "/")
        if observed_path != normalized_path:
            continue
        return observation.data.get("content_hash") == content_hash
    return False


def _compact_observation_history(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history = items[-MAX_OBSERVATIONS:]
    editor_names = {
        "read_file",
        "search_code",
        "retrieve_code_context",
        "edit_file_by_replace",
        "write_new_code",
    }
    detailed_indices = [
        index for index, item in enumerate(history) if item.get("name") in editor_names
    ][-MAX_DETAILED_EDITOR_OBSERVATIONS:]
    keep_details = set(detailed_indices)
    compacted: list[dict[str, Any]] = []
    for index, item in enumerate(history):
        current = json.loads(json.dumps(item))
        if current.get("name") in editor_names and index not in keep_details:
            data = current.get("data") if isinstance(current.get("data"), dict) else {}
            current["data"] = {
                key: data[key]
                for key in (
                    "path",
                    "content_hash",
                    "start_line",
                    "end_line",
                    "line_count",
                    "query",
                    "truncated",
                )
                if key in data
            }
            arguments = (
                current.get("arguments") if isinstance(current.get("arguments"), dict) else {}
            )
            current["arguments"] = {
                key: arguments[key]
                for key in ("path", "start_line", "end_line", "query", "path_filter")
                if key in arguments
            }
            current["summary"] = f"{current.get('name')} 已执行；详细正文已从短记忆压缩"
            current["truncated"] = True
        compacted.append(current)
    return compacted


def _engineering_context(
    root: Path,
    snapshot: dict[str, Any],
    checkpoint: dict[str, Any],
    observations: list[ToolExecutionResult],
) -> dict[str, Any]:
    """Build a compact context so the model can inspect by relevance, not enumeration."""
    file_index = checkpoint.get("workspace_file_index")
    if not isinstance(file_index, dict):
        file_index = _build_workspace_file_index(root)
    observed_files: list[str] = []
    for observation in observations:
        path = observation.arguments.get("path")
        if observation.name in {
            "read_file",
            "edit_file_by_replace",
            "write_new_code",
        } and isinstance(path, str):
            normalized = path.replace("\\", "/")
            if normalized not in observed_files:
                observed_files.append(normalized)
    return {
        "template_version": snapshot.get("template_version"),
        "stack": snapshot.get("stack") or {},
        "source_roots": {
            "frontend": "frontend/src",
            "backend": "backend/app",
            "api": "backend/app/api",
            "database_migrations": "backend/alembic/versions",
        },
        "workspace_file_index": file_index,
        "files_in_current_tool_history": observed_files,
        "files_modified_in_this_run": list(checkpoint.get("modified_files") or []),
        "long_term_memory": checkpoint.get("long_term_memory") or _new_long_term_memory(),
        "short_term_memory": {
            "observation_count": len(observations),
            "detailed_editor_observations": MAX_DETAILED_EDITOR_OBSERVATIONS,
        },
        "inspection_guidance": (
            "目录索引已经提供。根据当前功能、验收条件和交付物选择最小相关文件集；"
            "只有缺少具体契约或实现时才搜索或读取，不要按目录逐个浏览。"
        ),
    }


def _block_checkpoint(checkpoint: dict[str, Any], reason: str) -> None:
    checkpoint["outcome"] = "blocked"
    checkpoint["blocked_reason"] = reason
    activity = list(checkpoint.get("activity") or [])
    activity.append(
        {
            "id": f"blocked_{len(activity) + 1}",
            "name": "error",
            "label": "Blocked",
            "detail": reason[:200],
            "ok": False,
        }
    )
    checkpoint["activity"] = activity[-MAX_ACTIVITY:]


SessionFactory = Callable[[], Session]


class _WorkflowInputState(TypedDict):
    user_id: int
    project_id: int
    build_run_id: str
    task_id: str
    execution_id: str


class _WorkflowOutputState(TypedDict):
    project_id: int
    build_run_id: str
    task_id: str
    execution_id: str
    outcome: str
    current_work_item_id: str | None
    model_turns: int
    tool_calls: int


class _WorkflowState(_WorkflowInputState, total=False):
    outcome: str
    current_work_item_id: str | None
    model_turns: int
    tool_calls: int


class _WorkflowUpdate(TypedDict, total=False):
    outcome: str
    current_work_item_id: str | None
    model_turns: int
    tool_calls: int


def _workspace_root(project_id: int, run_id: str):
    return default_workspace_path(settings.runtime_data_root, project_id, run_id)


def _load_checkpoint(snapshot: dict[str, Any]) -> dict[str, Any]:
    checkpoint = snapshot.get("checkpoint")
    if isinstance(checkpoint, dict) and checkpoint.get("kind") == CHECKPOINT_KIND:
        checkpoint["schema_version"] = CHECKPOINT_SCHEMA_VERSION
        checkpoint.setdefault("long_term_memory", _new_long_term_memory())
        checkpoint.setdefault("writer_model_calls", 0)
        return checkpoint
    return {
        "kind": CHECKPOINT_KIND,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "work_items": [],
        "current_work_item_id": None,
        "model_turns": 0,
        "tool_calls": 0,
        "observations": [],
        "long_term_memory": _new_long_term_memory(),
        "writer_model_calls": 0,
        "modified_files": [],
        "blocked_reason": None,
        "outcome": "running",
        "activity": [],
    }


def _save_snapshot(db: Session, execution_id: str, snapshot: dict[str, Any]) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.task_execution import TaskExecution

    execution = db.get(TaskExecution, execution_id)
    if execution is None or execution.status != "running" or execution.active_slot != 1:
        raise ConflictException("执行编号已失效或任务正在由其他执行处理")
    if execution.expires_at <= utc_now():
        raise ConflictException("工程执行租约已过期，请显式恢复")
    # JSON columns do not detect in-place dict mutation; persist a new object.
    execution.draft = json.loads(json.dumps(snapshot))
    flag_modified(execution, "draft")
    db.commit()


def _paused_outcome(state: _WorkflowState) -> _WorkflowUpdate:
    return {
        "outcome": "paused",
        "current_work_item_id": state.get("current_work_item_id"),
        "model_turns": int(state.get("model_turns") or 0),
        "tool_calls": int(state.get("tool_calls") or 0),
    }


def _current_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in items:
        if item.get("status") == "pending":
            return item
    return None


class _EngineeringNodes:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def prepare(self, state: _WorkflowState) -> _WorkflowUpdate:
        with self._session_factory() as db:
            execution = latest_execution(db, state["task_id"], lock=True)
            if (
                execution is None
                or execution.execution_id != state["execution_id"]
                or execution.status != "running"
            ):
                raise ConflictException("工程执行不存在或已结束")
            snapshot = read_frozen_input_snapshot(execution)
            if snapshot is None:
                raise ConflictException("工程执行缺少冻结输入快照")
            _, _, _, spec = load_approved_app_spec(
                db,
                state["project_id"],
                state["build_run_id"],
                str(snapshot["approved_item_id"]),
                lock=True,
            )
            design_payload = snapshot.get("system_design")
            if snapshot.get("delivery_path") == "designed" and not isinstance(design_payload, dict):
                raise ConflictException("工程执行缺少冻结系统设计")
            if isinstance(design_payload, dict):
                SystemDesign.model_validate(design_payload)
            if not workspace_is_ready(state["project_id"], state["build_run_id"]):
                raise ConflictException("应用工作区尚未准备好")
            checkpoint = _load_checkpoint(snapshot)
            if not isinstance(checkpoint.get("workspace_file_index"), dict):
                checkpoint["workspace_file_index"] = _build_workspace_file_index(
                    _workspace_root(state["project_id"], state["build_run_id"])
                )
            blocked_reason = checkpoint.get("blocked_reason")
            if checkpoint.get("outcome") == "blocked" and blocked_reason in {
                "模型调用预算已用尽",
                "工具调用预算已用尽",
            }:
                # Explicit resume grants another budget window on the same frozen inputs.
                checkpoint["outcome"] = "running"
                checkpoint["blocked_reason"] = None
                checkpoint["model_turns"] = 0
                checkpoint["tool_calls"] = 0
                checkpoint.pop("last_error", None)
            if not checkpoint.get("work_items"):
                checkpoint["work_items"] = work_items_to_json(plan_delivery(spec))
            current = _current_item(checkpoint["work_items"])
            checkpoint["current_work_item_id"] = current["id"] if current else None
            if current is None:
                checkpoint["outcome"] = "generated"
            elif not checkpoint.get("activity"):
                checkpoint["activity"] = [
                    {
                        "id": "plan_approved",
                        "name": "summary",
                        "label": "Progress",
                        "detail": (
                            "计划已获批准，我现在初始化全栈项目模板（Vue 前端 + FastAPI 后端）。"
                        ),
                        "ok": True,
                    },
                    {
                        "id": "start_coding",
                        "name": "start",
                        "label": "Start coding",
                        "detail": str(current.get("title") or current["id"])[:200],
                        "ok": True,
                    },
                    {
                        "id": "engineering_context_ready",
                        "name": "summary",
                        "label": "Progress",
                        "detail": (
                            "工程上下文已准备完成：已载入技术栈、目录索引、"
                            "当前功能、验收条件和实现约束。"
                        ),
                        "ok": True,
                    },
                ]
            snapshot["checkpoint"] = checkpoint
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)
            return {
                "outcome": checkpoint["outcome"],
                "current_work_item_id": checkpoint["current_work_item_id"],
                "model_turns": checkpoint.get("model_turns") or 0,
                "tool_calls": checkpoint.get("tool_calls") or 0,
            }

    def step(self, state: _WorkflowState) -> _WorkflowUpdate:
        with self._session_factory() as db:
            execution = latest_execution(db, state["task_id"], lock=True)
            if (
                execution is None
                or execution.execution_id != state["execution_id"]
                or execution.status != "running"
                or execution.active_slot != 1
                or execution.expires_at <= utc_now()
            ):
                # Pause / supersede / lease expiry: stop cooperatively without crashing the worker.
                return _paused_outcome(state)
            snapshot = read_frozen_input_snapshot(execution)
            if snapshot is None:
                raise ConflictException("工程执行缺少冻结输入快照")
            checkpoint = _load_checkpoint(snapshot)
            if checkpoint.get("outcome") in {"generated", "blocked"}:
                return {
                    "outcome": checkpoint["outcome"],
                    "current_work_item_id": checkpoint.get("current_work_item_id"),
                    "model_turns": checkpoint.get("model_turns") or 0,
                    "tool_calls": checkpoint.get("tool_calls") or 0,
                }
            if int(checkpoint.get("model_turns") or 0) >= _call_budget(snapshot, "max_model_turns"):
                _block_checkpoint(checkpoint, "模型调用预算已用尽")
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                renew_execution_lease(db, state["task_id"], execution.execution_id)
                return {
                    "outcome": "blocked",
                    "current_work_item_id": checkpoint.get("current_work_item_id"),
                    "model_turns": checkpoint.get("model_turns") or 0,
                    "tool_calls": checkpoint.get("tool_calls") or 0,
                }
            if int(checkpoint.get("tool_calls") or 0) >= _call_budget(snapshot, "max_tool_calls"):
                _block_checkpoint(checkpoint, "工具调用预算已用尽")
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                renew_execution_lease(db, state["task_id"], execution.execution_id)
                return {
                    "outcome": "blocked",
                    "current_work_item_id": checkpoint.get("current_work_item_id"),
                    "model_turns": checkpoint.get("model_turns") or 0,
                    "tool_calls": checkpoint.get("tool_calls") or 0,
                }
            _, _, _, spec = load_approved_app_spec(
                db,
                state["project_id"],
                state["build_run_id"],
                str(snapshot["approved_item_id"]),
            )
            design_payload = snapshot.get("system_design")
            system_design = (
                SystemDesign.model_validate(design_payload)
                if isinstance(design_payload, dict)
                else None
            )
            if not checkpoint.get("work_items"):
                raise ConflictException("工程循环尚未规划工作单元")
            current_raw = _current_item(checkpoint["work_items"])
            if current_raw is None:
                checkpoint["outcome"] = "generated"
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                return {"outcome": "generated"}
            work_item = WorkItem.model_validate(current_raw)
            observations = [
                ToolExecutionResult.model_validate(item)
                for item in checkpoint.get("observations") or []
            ]
            root = _workspace_root(state["project_id"], state["build_run_id"])
            engineering_context = _engineering_context(
                root,
                snapshot,
                checkpoint,
                observations,
            )

        with self._session_factory() as db:
            execution = latest_execution(db, state["task_id"], lock=True)
            if (
                execution is None
                or execution.execution_id != state["execution_id"]
                or execution.status != "running"
                or execution.active_slot != 1
                or execution.expires_at <= utc_now()
            ):
                return _paused_outcome(state)
            snapshot = read_frozen_input_snapshot(execution)
            assert snapshot is not None
            checkpoint = _load_checkpoint(snapshot)
            waiting_id = f"model_{int(checkpoint.get('model_turns') or 0) + 1}"
            activity = list(checkpoint.get("activity") or [])
            activity.append(
                {
                    "id": waiting_id,
                    "name": "model",
                    "label": "Call model",
                    "detail": work_item.title[:200],
                    "ok": True,
                }
            )
            checkpoint["activity"] = activity[-MAX_ACTIVITY:]
            checkpoint.pop("last_error", None)
            snapshot["checkpoint"] = checkpoint
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)

        generated_file_content: str | None = None
        write_precondition_error: tuple[str, str] | None = None
        try:
            decision = decide_next_action(
                spec=spec,
                work_item=work_item,
                engineering_context=engineering_context,
                observations=observations,
                system_design=system_design,
            )
            if decision.tool_calls and decision.tool_calls[0].name == "write_new_code":
                write_call = decision.tool_calls[0]
                path = str(write_call.arguments.get("path") or "").strip()
                description = str(write_call.arguments.get("file_description") or "").strip()
                if not path or not description:
                    write_precondition_error = (
                        "INVALID_ARGUMENTS",
                        "生成完整文件必须提供 path 和 file_description",
                    )
                else:
                    target = safe_path_under_root(root, path, allow_create=True)
                    expected_hash = str(write_call.arguments.get("expected_hash") or "").strip()
                    if target.exists():
                        current_hash = sha256_bytes(target.read_bytes())
                        if not expected_hash:
                            write_precondition_error = (
                                "READ_REQUIRED",
                                "整体重写已有文件前必须先读取并提供 expected_hash",
                            )
                        elif expected_hash != current_hash:
                            write_precondition_error = (
                                "HASH_CONFLICT",
                                "目标文件已经变化，请重新读取后再生成",
                            )
                        elif not _has_current_file_evidence(
                            observations,
                            path=path,
                            content_hash=expected_hash,
                        ):
                            write_precondition_error = (
                                "READ_REQUIRED",
                                "整体重写已有文件前必须先用 read_file 读取当前版本",
                            )
                    elif expected_hash:
                        write_precondition_error = (
                            "HASH_CONFLICT",
                            "目标文件不存在，不能携带旧 expected_hash",
                        )
                    if write_precondition_error is None:
                        generated_file_content = generate_file_content(
                            spec=spec,
                            work_item=work_item,
                            path=path,
                            file_description=description,
                            engineering_context=engineering_context,
                            observations=observations,
                            system_design=system_design,
                        )
        except BusinessException as exc:
            with self._session_factory() as db:
                execution = latest_execution(db, state["task_id"], lock=True)
                if execution is None or execution.execution_id != state["execution_id"]:
                    raise ConflictException("工程执行不存在或已结束") from exc
                snapshot = read_frozen_input_snapshot(execution)
                assert snapshot is not None
                checkpoint = _load_checkpoint(snapshot)
                checkpoint["last_error"] = str(exc)[:500]
                activity = list(checkpoint.get("activity") or [])
                if activity and activity[-1].get("name") == "model":
                    activity[-1] = {**activity[-1], "ok": False, "detail": str(exc)[:200]}
                else:
                    activity.append(
                        {
                            "id": f"error_{len(activity) + 1}",
                            "name": "error",
                            "label": "Blocked",
                            "detail": str(exc)[:200],
                            "ok": False,
                        }
                    )
                checkpoint["activity"] = activity[-MAX_ACTIVITY:]
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                renew_execution_lease(db, state["task_id"], execution.execution_id)
            return {
                "outcome": "blocked",
                "current_work_item_id": checkpoint.get("current_work_item_id"),
                "model_turns": checkpoint.get("model_turns") or 0,
                "tool_calls": checkpoint.get("tool_calls") or 0,
            }
        with self._session_factory() as db:
            execution = latest_execution(db, state["task_id"], lock=True)
            if execution is None or execution.execution_id != state["execution_id"]:
                raise ConflictException("工程执行不存在或已结束")
            snapshot = read_frozen_input_snapshot(execution)
            assert snapshot is not None
            checkpoint = _load_checkpoint(snapshot)
            checkpoint["model_turns"] = int(checkpoint.get("model_turns") or 0) + 1
            if generated_file_content is not None:
                checkpoint["writer_model_calls"] = (
                    int(checkpoint.get("writer_model_calls") or 0) + 1
                )
            checkpoint.pop("last_error", None)
            if not decision.tool_calls:
                checkpoint["outcome"] = "blocked"
                checkpoint["blocked_reason"] = "模型没有提出工具调用"
                snapshot["checkpoint"] = checkpoint
                _save_snapshot(db, execution.execution_id, snapshot)
                renew_execution_lease(db, state["task_id"], execution.execution_id)
                return {"outcome": "blocked", "model_turns": checkpoint["model_turns"]}

            call = decision.tool_calls[0]
            activity = list(checkpoint.get("activity") or [])
            activity.append(
                {
                    "id": f"summary_{checkpoint['model_turns']}",
                    "name": "summary",
                    "label": "Progress",
                    "detail": _progress_summary(decision.content, call, work_item),
                    "ok": True,
                }
            )
            checkpoint["activity"] = activity[-MAX_ACTIVITY:]
            snapshot["checkpoint"] = checkpoint
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)

            def complete_work_item(args: dict[str, Any]) -> ToolExecutionResult:
                work_item_id = str(args.get("work_item_id") or "")
                current = _current_item(checkpoint["work_items"])
                if current is None or current["id"] != work_item_id:
                    return ToolExecutionResult(
                        tool_call_id=call.id,
                        name="complete_work_item",
                        ok=False,
                        error_code="WRONG_WORK_ITEM",
                        summary="只能结束当前工作单元",
                    )
                last_check = checkpoint.get("last_successful_check")
                if not isinstance(last_check, dict) or (
                    last_check.get("work_item_id"),
                    last_check.get("check_id"),
                ) != (work_item_id, "all"):
                    return ToolExecutionResult(
                        tool_call_id=call.id,
                        name="complete_work_item",
                        ok=False,
                        error_code="CHECK_REQUIRED",
                        summary="结束工作单元前必须对最新源码运行 all 检查",
                    )
                try:
                    current_hash = check_tools.source_snapshot(root)[1]
                except (BusinessException, OSError, UnicodeError) as exc:
                    return ToolExecutionResult(
                        tool_call_id=call.id,
                        name="complete_work_item",
                        ok=False,
                        error_code="INVALID_SOURCE",
                        summary=str(exc),
                    )
                if last_check.get("source_hash") != current_hash:
                    return ToolExecutionResult(
                        tool_call_id=call.id,
                        name="complete_work_item",
                        ok=False,
                        error_code="CHECK_STALE",
                        summary="源码在检查后发生变化，请重新运行 all 检查",
                    )
                current["status"] = "completed"
                current["summary"] = str(args.get("summary") or "")[:500]
                next_item = _current_item(checkpoint["work_items"])
                checkpoint["current_work_item_id"] = next_item["id"] if next_item else None
                checkpoint["observations"] = []
                checkpoint.pop("last_successful_check", None)
                if next_item is None:
                    checkpoint["outcome"] = "generated"
                return ToolExecutionResult(
                    tool_call_id=call.id,
                    name="complete_work_item",
                    ok=True,
                    summary=f"工作单元 {work_item_id} 已记录为写入完成",
                    data={"next_work_item_id": checkpoint["current_work_item_id"]},
                )

            def report_blocked(args: dict[str, Any]) -> ToolExecutionResult:
                reason = str(args.get("reason") or "需求无法满足")
                checkpoint["outcome"] = "blocked"
                checkpoint["blocked_reason"] = reason[:500]
                return ToolExecutionResult(
                    tool_call_id=call.id,
                    name="report_blocked",
                    ok=True,
                    summary=reason[:200],
                )

            def write_new_code(args: dict[str, Any]) -> ToolExecutionResult:
                if write_precondition_error is not None:
                    error_code, summary = write_precondition_error
                    return ToolExecutionResult(
                        tool_call_id=call.id,
                        name="write_new_code",
                        ok=False,
                        error_code=error_code,
                        summary=summary,
                    )
                if generated_file_content is None:
                    return ToolExecutionResult(
                        tool_call_id=call.id,
                        name="write_new_code",
                        ok=False,
                        error_code="GENERATION_FAILED",
                        summary="写码模型没有生成文件内容",
                    )
                written = file_tools.apply_patch(
                    root,
                    path=str(args.get("path") or ""),
                    content=generated_file_content,
                    expected_hash=(
                        str(args["expected_hash"]) if args.get("expected_hash") else None
                    ),
                    tool_call_id=call.id,
                )
                return written.model_copy(
                    update={
                        "name": "write_new_code",
                        "summary": (
                            f"已生成并写入 {str(args.get('path') or '')}"
                            if written.ok
                            else written.summary
                        ),
                    }
                )

            def record_engineering_memory(args: dict[str, Any]) -> ToolExecutionResult:
                subject = str(args.get("subject") or "").strip()
                fact = str(args.get("fact") or "").strip()
                raw_paths = args.get("evidence_paths")
                evidence_paths = (
                    [str(path).replace("\\", "/").strip() for path in raw_paths]
                    if isinstance(raw_paths, list)
                    else []
                )
                if not subject or not fact or not evidence_paths:
                    return ToolExecutionResult(
                        tool_call_id=call.id,
                        name="record_engineering_memory",
                        ok=False,
                        error_code="INVALID_MEMORY",
                        summary="长期工程事实必须包含 subject、fact 和 evidence_paths",
                    )
                memory = checkpoint.setdefault("long_term_memory", _new_long_term_memory())
                files = memory.get("files") if isinstance(memory, dict) else None
                files = files if isinstance(files, dict) else {}
                missing = [path for path in evidence_paths if path not in files]
                if missing:
                    return ToolExecutionResult(
                        tool_call_id=call.id,
                        name="record_engineering_memory",
                        ok=False,
                        error_code="UNVERIFIED_MEMORY",
                        summary=f"请先读取或写入证据文件：{', '.join(missing[:3])}",
                    )
                facts = memory.get("facts")
                facts = facts if isinstance(facts, list) else []
                entry = {
                    "subject": subject[:120],
                    "fact": fact[:600],
                    "evidence_paths": evidence_paths[:8],
                    "evidence_hashes": {
                        path: (
                            files[path].get("content_hash")
                            if isinstance(files[path], dict)
                            else None
                        )
                        for path in evidence_paths[:8]
                    },
                    "work_item_id": work_item.id,
                }
                facts = [
                    item
                    for item in facts
                    if not isinstance(item, dict) or item.get("subject") != entry["subject"]
                ]
                facts.append(entry)
                memory["facts"] = facts[-MAX_MEMORY_FACTS:]
                return ToolExecutionResult(
                    tool_call_id=call.id,
                    name="record_engineering_memory",
                    ok=True,
                    summary=f"已记录工程事实：{entry['subject']}",
                    data={"subject": entry["subject"], "evidence_paths": evidence_paths[:8]},
                )

            def retrieve_code_context(args: dict[str, Any]) -> ToolExecutionResult:
                if not any(
                    observation.name == "search_code" and observation.ok
                    for observation in observations
                ):
                    return ToolExecutionResult(
                        tool_call_id=call.id,
                        name="retrieve_code_context",
                        ok=False,
                        error_code="EXACT_SEARCH_REQUIRED",
                        summary="使用 RAG 前必须先调用 search_code 做精确搜索",
                    )
                return retrieve_workspace_context(
                    root,
                    query=str(args.get("query") or ""),
                    path_filter=(str(args["path_filter"]) if args.get("path_filter") else None),
                    max_results=int(args.get("max_results") or 5),
                    tool_call_id=call.id,
                )

            if call.name == "edit_file_by_replace" and not _has_current_file_evidence(
                observations,
                path=str(call.arguments.get("path") or ""),
                content_hash=str(call.arguments.get("expected_hash") or ""),
            ):
                result = ToolExecutionResult(
                    tool_call_id=call.id,
                    name="edit_file_by_replace",
                    ok=False,
                    error_code="READ_REQUIRED",
                    summary="局部修改已有文件前必须先用 read_file 读取当前版本",
                )
            else:
                result = execute_tool_call(
                    call,
                    workspace_root=root,
                    complete_work_item=complete_work_item,
                    report_blocked=report_blocked,
                    write_new_code=write_new_code,
                    record_engineering_memory=record_engineering_memory,
                    retrieve_code_context=retrieve_code_context,
                )
            result = result.model_copy(update={"arguments": dict(call.arguments)})
            checkpoint["tool_calls"] = int(checkpoint.get("tool_calls") or 0) + 1
            if call.name in {"edit_file_by_replace", "write_new_code"} and result.ok:
                checkpoint.pop("last_successful_check", None)
                path = str(call.arguments.get("path") or "").replace("\\", "/")
                modified_files = list(checkpoint.get("modified_files") or [])
                if path and path not in modified_files:
                    modified_files.append(path)
                checkpoint["modified_files"] = modified_files[-80:]
                file_index = checkpoint.get("workspace_file_index")
                if isinstance(file_index, dict):
                    indexed_paths = list(file_index.get("paths") or [])
                    if (
                        path
                        and path not in indexed_paths
                        and len(indexed_paths) < MAX_CONTEXT_FILES
                    ):
                        indexed_paths.append(path)
                        file_index["paths"] = sorted(indexed_paths)
                        file_index["visible_file_count"] = max(
                            int(file_index.get("visible_file_count") or 0),
                            len(indexed_paths),
                        )
            elif call.name == "run_check":
                if result.ok and call.arguments.get("check_id") == "all":
                    current = _current_item(checkpoint["work_items"])
                    source_hash = result.data.get("source_hash")
                    if current is not None and isinstance(source_hash, str):
                        checkpoint["last_successful_check"] = {
                            "work_item_id": current["id"],
                            "check_id": "all",
                            "source_hash": source_hash,
                        }
                else:
                    checkpoint.pop("last_successful_check", None)
            _update_file_memory(checkpoint, result, work_item.id)
            saved_observations: list[dict[str, Any]] = list(checkpoint.get("observations") or [])
            saved_observations.append(result.model_dump(mode="json"))
            checkpoint["observations"] = _compact_observation_history(saved_observations)
            activity = list(checkpoint.get("activity") or [])
            activity.append(_activity_entry(call, result))
            checkpoint["activity"] = activity[-MAX_ACTIVITY:]
            snapshot["checkpoint"] = checkpoint
            _save_snapshot(db, execution.execution_id, snapshot)
            renew_execution_lease(db, state["task_id"], execution.execution_id)
            return {
                "outcome": checkpoint.get("outcome") or "running",
                "current_work_item_id": checkpoint.get("current_work_item_id"),
                "model_turns": checkpoint.get("model_turns") or 0,
                "tool_calls": checkpoint.get("tool_calls") or 0,
            }


def run_engineering_workflow(
    db: Session,
    user: User,
    project_id: int,
    run_id: str,
    task_id: str,
    execution_id: str,
    *,
    session_factory: SessionFactory | None = None,
) -> _WorkflowOutputState:
    if session_factory is None:
        if db.new or db.dirty or db.deleted:
            raise BusinessException("请先保存调用方待提交的修改，再启动工程循环")
        db.rollback()
        session_factory = cast(
            SessionFactory,
            sessionmaker(
                bind=db.get_bind(),
                autocommit=False,
                autoflush=False,
                expire_on_commit=False,
            ),
        )
    nodes = _EngineeringNodes(session_factory)
    state: _WorkflowState = {
        "user_id": user.id,
        "project_id": project_id,
        "build_run_id": run_id,
        "task_id": task_id,
        "execution_id": execution_id,
    }
    try:
        state.update(nodes.prepare(state))
        while state.get("outcome") not in {"generated", "blocked", "paused"}:
            state.update(nodes.step(state))
    except ConflictException:
        # Pause, lease expiry, or fencing stopped this attempt; keep plan/task for resume.
        state["outcome"] = "paused"
    return {
        "project_id": project_id,
        "build_run_id": run_id,
        "task_id": task_id,
        "execution_id": execution_id,
        "outcome": str(state.get("outcome") or "running"),
        "current_work_item_id": state.get("current_work_item_id"),
        "model_turns": int(state.get("model_turns") or 0),
        "tool_calls": int(state.get("tool_calls") or 0),
    }
