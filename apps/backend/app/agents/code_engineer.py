"""Code Engineer 与模型交互：在冻结需求下规划文件、写码并选择下一步工具。"""

from __future__ import annotations

import ast
import json
import tomllib
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.agents.prompts.code_engineer import (
    CODE_ENGINEER_SYSTEM_PROMPT,
    CODE_WRITER_SYSTEM_PROMPT,
    FILE_PLANNER_SYSTEM_PROMPT,
    REPAIR_PLANNER_SYSTEM_PROMPT,
)
from app.agents.roles import get_role_profile
from app.core.exceptions import BusinessException
from app.core.llm import chat_completion, chat_with_tools
from app.core.llm_response import CompletionContentError
from app.core.settings import settings
from app.generation.delivery import MAX_FILES_PER_WORK_ITEM, FileTask, ImplementationPlan, WorkItem
from app.models.task import TaskRecipient
from app.schemas.agent_action import ChatWithToolsResult, ToolDefinition, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.tools.code_engineer import CODE_ENGINEER_TOOLS as ALL_CODE_ENGINEER_TOOLS

CODE_ENGINEER_PROFILE = get_role_profile(TaskRecipient.CODE_ENGINEER)
# 只保留角色白名单内的工具定义。
CODE_ENGINEER_TOOLS = [
    tool for tool in ALL_CODE_ENGINEER_TOOLS if tool.name in CODE_ENGINEER_PROFILE.allowed_tools
]

# 同一文件在拆包/语法/协议校验失败后的重生次数（含首次）。
MAX_WRITER_ATTEMPTS = 3


class FileGenerationError(BusinessException):
    """写码校验耗尽；携带本次已消耗的模型调用次数。"""

    def __init__(self, message: str, *, model_calls: int) -> None:
        super().__init__(message)
        self.model_calls = model_calls


@dataclass(frozen=True, slots=True)
class PlannedFileBatch:
    """本批可执行文件计划，以及留给后续批次的溢出文件。"""

    plan: ImplementationPlan
    deferred_files: tuple[FileTask, ...] = ()


def _unwrap_json_object(value: str) -> dict[str, Any]:
    """从模型回复中抽出 JSON 对象（兼容 Markdown 代码围栏与前后杂质）。"""

    text = value.strip()
    if not text:
        raise BusinessException("文件级实施规划器没有返回合法 JSON")
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        sliced = text[start : end + 1].strip()
        if sliced not in candidates:
            candidates.append(sliced)
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(payload, dict):
            return payload
        last_error = ValueError("JSON 根节点不是对象")
    raise BusinessException("文件级实施规划器没有返回合法 JSON") from last_error


def _planner_messages(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    workspace_file_index: dict[str, Any],
    system_design: SystemDesign | None,
    repair_context: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """组装文件规划器对话；有修复上下文时改用修复规划 prompt。"""

    return [
        {
            "role": "system",
            "content": (
                REPAIR_PLANNER_SYSTEM_PROMPT
                if repair_context is not None
                else FILE_PLANNER_SYSTEM_PROMPT
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "product_intent": spec.model_dump(mode="json"),
                    "current_work_item": work_item.model_dump(mode="json"),
                    "system_design": (
                        system_design.model_dump(mode="json") if system_design is not None else None
                    ),
                    "workspace_file_index": workspace_file_index,
                    "repair_context": repair_context,
                    "constraints": {
                        "max_files_per_work_item": MAX_FILES_PER_WORK_ITEM,
                        "files_array_length": f"1..{MAX_FILES_PER_WORK_ITEM}",
                        "prefer_at_most": min(12, MAX_FILES_PER_WORK_ITEM),
                        "image_generation_available": settings.image_generation_enabled,
                        "image_provider": "Volcengine Ark / Seedream",
                        "image_default_size": "2K",
                    },
                    "output_schema": ImplementationPlan.model_json_schema(),
                },
                ensure_ascii=False,
            ),
        },
    ]


def _validate_implementation_plan(raw: str, *, work_item: WorkItem) -> PlannedFileBatch:
    """解析并校验规划器输出，得到本批计划与延期文件。"""

    payload, deferred = _normalize_planner_payload(_unwrap_json_object(raw), work_item=work_item)
    try:
        plan = ImplementationPlan.model_validate(payload)
    except ValidationError as exc:
        errors = []
        for item in exc.errors()[:5]:
            location = ".".join(str(part) for part in item.get("loc", ()))
            message = str(item.get("msg") or "字段无效")
            errors.append(f"{location}: {message}" if location else message)
        detail = "；".join(errors) or "未知字段错误"
        raise BusinessException(f"文件级实施计划结构不合法：{detail}") from exc
    plan.validate_unique_paths()
    deferred_tasks = tuple(FileTask.model_validate(item) for item in deferred)
    return PlannedFileBatch(plan=plan, deferred_files=deferred_tasks)


def _normalize_planner_payload(
    payload: dict[str, Any], *, work_item: WorkItem
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """容忍供应商无害字段差异，但执行范围仍由平台控制（批大小、去重、补全）。"""

    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise BusinessException("文件级实施计划结构不合法：files 必须是非空数组")

    files: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index, raw_file in enumerate(raw_files, start=1):
        if not isinstance(raw_file, dict):
            raise BusinessException(f"文件级实施计划结构不合法：files.{index - 1} 必须是对象")
        raw_path = raw_file.get("path") or raw_file.get("target_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise BusinessException(f"文件级实施计划结构不合法：files.{index - 1}.path 不能为空")
        path = raw_path.replace("\\", "/").strip()
        if path in seen_paths:
            continue
        seen_paths.add(path)

        raw_description = (
            raw_file.get("description")
            or raw_file.get("file_description")
            or raw_file.get("responsibility")
        )
        description = (
            raw_description.strip()
            if isinstance(raw_description, str) and raw_description.strip()
            else f"实现 {path} 在当前工作单元中的职责"
        )
        raw_contexts = raw_file.get("context_paths") or raw_file.get("dependencies") or []
        contexts: list[str] = []
        if isinstance(raw_contexts, list):
            for raw_context in raw_contexts:
                if not isinstance(raw_context, str):
                    continue
                context = raw_context.replace("\\", "/").strip()
                if not context or context == path or context in contexts:
                    continue
                contexts.append(context)
                if len(contexts) == 12:
                    break
        entry = {
            "id": f"file_{len(files) + len(deferred) + 1:02d}",
            "path": path,
            "description": description,
            "kind": "image" if raw_file.get("kind") == "image" else "source",
            "image_size": raw_file.get("image_size", "2K"),
            "image_watermark": raw_file.get("image_watermark", True),
            "context_paths": contexts,
            "status": "pending",
            "content_hash": None,
        }
        if len(files) >= MAX_FILES_PER_WORK_ITEM:
            # 超出本批上限的文件延后，等本批检查通过后再写。
            deferred.append(entry)
            continue
        files.append(entry)
    # 延期项单独编号，便于后续批次提升时重新编号。
    for offset, item in enumerate(deferred, start=1):
        item["id"] = f"deferred_{offset:02d}"
    if not files:
        raise BusinessException("文件级实施计划结构不合法：没有可执行的唯一目标文件")
    raw_summary = payload.get("summary")
    summary = (
        raw_summary.strip()
        if isinstance(raw_summary, str) and raw_summary.strip()
        else f"实现：{work_item.title}"
    )
    if deferred:
        summary = (
            f"{summary.rstrip()}（本批执行前 {len(files)} 个文件；"
            f"其余 {len(deferred)} 个将在本批检查通过后续写）"
        )
    return (
        {
            "work_item_id": work_item.id,
            "summary": summary[:2000],
            "files": files,
        },
        deferred,
    )


def plan_work_item_files(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    workspace_file_index: dict[str, Any],
    system_design: SystemDesign | None,
    repair_context: dict[str, Any] | None = None,
) -> PlannedFileBatch:
    """把一个已批准工作单元变成有序、已校验的文件实施计划。"""

    messages = _planner_messages(
        spec=spec,
        work_item=work_item,
        workspace_file_index=workspace_file_index,
        system_design=system_design,
        repair_context=repair_context,
    )
    last_error: BusinessException | None = None
    for attempt in range(3):
        raw = chat_completion(
            messages=list(messages),
            temperature=0.1 if attempt == 0 else 0.0,
            max_tokens=8192,
            json_output=True,
        )
        try:
            return _validate_implementation_plan(raw, work_item=work_item)
        except BusinessException as exc:
            last_error = exc
            if attempt == 2:
                raise
        # 把具体校验错误贴回去重试；模糊重提容易让模型原样再错一遍。
        messages.extend(
            [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "上一次输出不是符合 output_schema 的单个 JSON 对象。"
                        f"具体校验结果：{last_error}。"
                        f"work_item_id 必须是 {work_item.id}，files 为 1 到 "
                        f"{MAX_FILES_PER_WORK_ITEM} 个（更少更好）；"
                        "每个文件必须包含 path、description、context_paths。"
                        "请只重新输出完整 JSON，不要使用 Markdown 围栏或解释文字。"
                    ),
                },
            ]
        )
    assert last_error is not None
    raise last_error


def _validate_generated_file(path: str, content: str) -> None:
    """写盘前做廉价语法检查，尽早拦下明显坏内容。"""

    try:
        if path.lower().endswith(".py"):
            ast.parse(content, filename=path)
        elif path.lower().endswith(".json"):
            json.loads(content)
        elif path.lower().endswith(".toml"):
            tomllib.loads(content)
    except (SyntaxError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        line = getattr(exc, "lineno", None)
        location = f"（第 {line} 行）" if isinstance(line, int) else ""
        raise BusinessException(f"写码模型返回的 {path} 语法无效{location}：{exc}") from exc


def _normalize_source_candidate(value: str, *, path: str) -> str:
    """去掉围栏、补换行，并做语法校验后返回可写入内容。"""

    content = value.strip()
    if content.startswith("```") and content.endswith("```"):
        first_newline = content.find("\n")
        if first_newline != -1:
            content = content[first_newline + 1 : -3].strip()
    if not content or "\x00" in content:
        raise BusinessException("写码模型没有返回有效文件内容")
    normalized = content + ("" if content.endswith("\n") else "\n")
    _validate_generated_file(path, normalized)
    return normalized


def generate_file_content(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    path: str,
    file_description: str,
    engineering_context: dict[str, Any],
    observations: list[ToolExecutionResult],
    system_design: SystemDesign | None,
) -> tuple[str, int]:
    """根据平台组装的上下文生成恰好一个完整文件。

    返回 ``(content, model_calls)``。协议或语法失败时对同一文件重生，
    最多 ``MAX_WRITER_ATTEMPTS`` 次后抛错。
    """

    recent_memory = [observation.model_dump(mode="json") for observation in observations]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": CODE_WRITER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "goal": spec.goal,
                    "constraints": [item.model_dump(mode="json") for item in spec.constraints],
                    "current_work_item": work_item.model_dump(mode="json"),
                    "system_design": (
                        system_design.model_dump(mode="json") if system_design is not None else None
                    ),
                    "target_path": path,
                    "file_description": file_description,
                    "engineering_context": engineering_context,
                    "recent_memory": recent_memory,
                },
                ensure_ascii=False,
            ),
        },
    ]
    last_error: BusinessException | None = None
    model_calls = 0
    for attempt in range(MAX_WRITER_ATTEMPTS):
        raw = ""
        try:
            raw = chat_completion(
                messages=list(messages),
                temperature=0.2 if attempt == 0 else 0.0,
                max_tokens=8192,
                content_kind="source",
            )
        except CompletionContentError as exc:
            model_calls += 1
            last_error = exc
        else:
            model_calls += 1
            try:
                return _normalize_source_candidate(raw, path=path), model_calls
            except BusinessException as exc:
                last_error = exc
        assert last_error is not None
        if attempt + 1 >= MAX_WRITER_ATTEMPTS:
            break
        messages.extend(
            [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"上一次 {path} 输出不能写入（第 {attempt + 1}/"
                        f"{MAX_WRITER_ATTEMPTS} 次）：{last_error}。"
                        "请按系统约定重新输出唯一的 whole-file 代码围栏，不得包含解释文字。"
                    ),
                },
            ]
        )
    assert last_error is not None
    raise FileGenerationError(str(last_error), model_calls=model_calls) from last_error


def build_code_engineer_messages(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    engineering_context: dict[str, Any],
    observations: list[ToolExecutionResult],
    system_design: SystemDesign | None,
) -> list[dict[str, Any]]:
    """组装工具对话历史：每条工具结果紧跟对应的 assistant tool_calls。"""

    history: list[dict[str, Any]] = [
        {"role": "system", "content": CODE_ENGINEER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "product_intent": {
                        "goal": spec.goal,
                        "constraints": [item.model_dump(mode="json") for item in spec.constraints],
                        "features": [item.model_dump(mode="json") for item in spec.features],
                    },
                    "current_work_item": work_item.model_dump(mode="json"),
                    "acceptance_criteria": [
                        item.model_dump(mode="json") for item in spec.acceptance_criteria
                    ],
                    "system_design": (
                        system_design.model_dump(mode="json") if system_design is not None else None
                    ),
                    "delivery_path": "designed" if system_design is not None else "direct",
                    "engineering_context": engineering_context,
                },
                ensure_ascii=False,
            ),
        },
    ]
    # 把已执行工具的观察结果回放进历史，供模型决定下一步。
    for observation in observations:
        history.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": observation.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": observation.name,
                            "arguments": json.dumps(observation.arguments, ensure_ascii=False),
                        },
                    }
                ],
            }
        )
        history.append(
            {
                "role": "tool",
                "tool_call_id": observation.tool_call_id,
                "name": observation.name,
                "content": json.dumps(observation.model_dump(mode="json"), ensure_ascii=False)[
                    :8000
                ],
            }
        )
    return history


def decide_next_action(
    *,
    spec: AppSpec,
    work_item: WorkItem,
    engineering_context: dict[str, Any],
    observations: list[ToolExecutionResult],
    system_design: SystemDesign | None,
    tools: list[ToolDefinition] | None = None,
) -> ChatWithToolsResult:
    """让模型选择下一步工具；只保留第一个工具调用。"""

    available_tools = tools or CODE_ENGINEER_TOOLS
    result = chat_with_tools(
        messages=build_code_engineer_messages(
            spec=spec,
            work_item=work_item,
            engineering_context=engineering_context,
            observations=observations,
            system_design=system_design,
        ),
        tools=available_tools,
    )
    if not result.tool_calls:
        raise BusinessException("Code Engineer 未调用工具推进当前工作单元")
    call = result.tool_calls[0]
    if call.name not in {tool.name for tool in available_tools}:
        raise BusinessException(f"Code Engineer 无权使用工具：{call.name}")
    return result.model_copy(update={"tool_calls": [call]})
