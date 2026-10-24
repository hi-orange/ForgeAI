"""Stage tool side effects, then commit only inside the execution fence."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.core.exceptions import AppException, ConflictException
from app.generation.engineering.smoke_guard import (
    smoke_probe_inventory_violation,
    smoke_probe_violation,
)
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.tools import files
from app.tools.code_engineer import (
    MUTATING_TOOLS,
    execute_tool_call,
    rejected_tool,
    validate_tool_call,
)
from app.tools.paths import safe_path_under_root, sha256_bytes
from sandbox.smoke_manifest import SMOKE_CONTRACT, parse_smoke_manifest

PROTECTED_ACCEPTANCE_PATHS = (
    "backend/tests/acceptance/",
    "frontend/src/acceptance/",
    "frontend/e2e/",
    "forgeai.acceptance.json",
)
_VISUAL_FEEDBACK_FIELDS = frozenset(
    {"theme_file", "theme_mode", "theme_reason", "required_tokens", "local_assets"}
)
_VISUAL_FEEDBACK_MAX_CHARS = 1500
_VISUAL_FEEDBACK_MAX_BYTES = 4096


@dataclass
class PreparedAction:
    result: ToolExecutionResult
    originals: dict[str, bytes | None] = field(default_factory=dict)
    updates: dict[str, bytes] = field(default_factory=dict)
    deletions: set[str] = field(default_factory=set)
    input_source_hash: str | None = None


def writable_path(root: Path, path: str) -> str:
    target = safe_path_under_root(root, path, allow_create=True)
    canonical = target.relative_to(root.resolve()).as_posix()
    folded = canonical.casefold()
    if any(
        folded == prefix.rstrip("/").casefold() or folded.startswith(prefix.casefold())
        for prefix in PROTECTED_ACCEPTANCE_PATHS
    ):
        raise ConflictException("Code Engineer 不能修改冻结验收资产")
    return canonical


def _visual_manifest_rejection(
    call: ToolCall,
    error: ValueError,
    *,
    original: bytes | None,
    root: Path,
) -> ToolExecutionResult:
    """Offer a bounded, previously validated visual contract as repair evidence."""
    message = str(error)
    if not original or not any(
        marker in message for marker in ("visual_contract", "视觉合同", "源码路径")
    ):
        return rejected_tool(call, error)
    try:
        metadata = json.loads(safe_path_under_root(root, "template.json").read_bytes())
        if (
            not isinstance(metadata, dict)
            or metadata.get("template_version") != "fullstack-react-v1"
        ):
            return rejected_tool(call, error)
        current = parse_smoke_manifest(
            original.decode("utf-8"), root=root.resolve(), template_metadata=metadata
        )
        visual = current.get("visual_contract")
        if not isinstance(visual, dict) or not visual or set(visual) - _VISUAL_FEEDBACK_FIELDS:
            return rejected_tool(call, error)
        exact_json = json.dumps(visual, ensure_ascii=False, separators=(",", ":"))
        if (
            len(exact_json) > _VISUAL_FEEDBACK_MAX_CHARS
            or len(exact_json.encode("utf-8")) > _VISUAL_FEEDBACK_MAX_BYTES
        ):
            return rejected_tool(call, error)
    except (OSError, UnicodeError, ValueError, TypeError):
        return rejected_tool(call, error)
    hint = (
        "保留下面结构校验已通过的原 visual_contract；只修改需要更新的 api_checks/browser_checks。"
    )
    if len(current["api_checks"]) >= SMOKE_CONTRACT["limits"]["api_checks"]:
        hint += "API 探针已达上限，请替换或合并旧探针。"
    summary = f"{message[:300]}；{hint} 原 visual_contract={exact_json}"
    if len(summary) > 2000:
        return rejected_tool(call, error)
    result = rejected_tool(call, ValueError(summary))
    return result.model_copy(
        update={
            "data": {
                **result.data,
                "current_valid_visual_contract": visual,
                "repair_hint": hint,
            }
        }
    )


def prepare_action(
    call: ToolCall,
    root: Path,
    observed_hashes: dict[str, str],
    *,
    checkpoint: dict | None = None,
) -> PreparedAction:
    """Network/model/container work happens here, outside database transactions."""
    try:
        call = validate_tool_call(call)
        if call.name not in MUTATING_TOOLS:
            return PreparedAction(execute_tool_call(call, workspace_root=root))
        if call.name == "install_capability":
            raise ConflictException("能力安装必须通过冻结输入授权的专用暂存入口")
        if call.name == "generate_api_client":
            from app.tools.api_client import prepare_client

            return prepare_client(call, root)
        writes = None
        if call.name == "write_files":
            writes = call.arguments["writes"]
        elif call.name == "write_file":  # Historical calls use the same staging path.
            writes = [call.arguments]
        if writes is not None:
            paths: list[str] = []
            for row in writes:
                row["path"] = writable_path(root, row["path"])
                if row["path"].casefold() in {p.casefold() for p in paths}:
                    raise ConflictException("批量写入不能包含重复路径")
                paths.append(row["path"])
        elif call.name == "install_project_dependency":
            paths = (
                ["frontend/package.json", "frontend/package-lock.json"]
                if call.arguments["manager"] == "npm"
                else ["backend/pyproject.toml", "backend/uv.lock"]
            )
        else:
            path = writable_path(root, call.arguments["path"])
            call.arguments["path"] = path
            paths = [path]
            if call.name == "generate_image":
                paths = list(
                    dict.fromkeys(
                        [
                            path,
                            *(
                                str(Path(path).with_suffix(s)).replace("\\", "/")
                                for s in (".png", ".jpg", ".webp")
                            ),
                        ]
                    )
                )
        originals: dict[str, bytes | None] = {}
        for path in paths:
            canonical = writable_path(root, path)
            target = safe_path_under_root(root, canonical, allow_create=True)
            originals[canonical] = target.read_bytes() if target.exists() else None
        if call.name == "edit_file_by_replace":
            path = call.arguments["path"]
            if originals[path] is not None and (
                not call.arguments.get("expected_hash")
                or observed_hashes.get(path) != call.arguments["expected_hash"]
            ):
                raise ConflictException(
                    "修改已有源码缺少匹配的已观察 content_hash；"
                    "使用初始源码上下文或工作记忆中完整、当前的内容与哈希，"
                    "缺失或过期时再定向 read_file"
                )
        if writes is not None:
            for row in writes:
                if originals[row["path"]] is not None and (
                    not row.get("expected_hash")
                    or observed_hashes.get(row["path"]) != row["expected_hash"]
                ):
                    raise ConflictException(
                        f"修改 {row['path']} 缺少匹配的已观察 content_hash；"
                        "使用初始源码上下文或工作记忆中完整、当前的内容与哈希，"
                        "缺失或过期时再定向 read_file"
                    )
        with TemporaryDirectory(prefix="forgeai-tool-stage-") as directory:
            staging = Path(directory)
            for path, original in originals.items():
                if original is not None:
                    target = staging / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(original)
            result = execute_tool_call(call, workspace_root=staging)
            updates = {}
            if result.ok:
                for path, original in originals.items():
                    staged = staging / path
                    if staged.is_file():
                        content = staged.read_bytes()
                        if content != original:
                            updates[path] = content
                if "forgeai.smoke.json" in updates:
                    try:
                        candidate = json.loads(updates["forgeai.smoke.json"])
                    except ValueError:
                        candidate = None  # The full parser gives the syntax error.
                    if checkpoint is not None:
                        violation = smoke_probe_inventory_violation(checkpoint, candidate)
                        if violation is not None:
                            raise ConflictException(violation)
                    metadata = updates.get("template.json")
                    if metadata is None:
                        metadata = safe_path_under_root(root, "template.json").read_bytes()
                    try:
                        manifest = parse_smoke_manifest(
                            updates["forgeai.smoke.json"].decode("utf-8"),
                            root=root.resolve(),
                            template_metadata=json.loads(metadata),
                        )
                    except ValueError as exc:
                        return PreparedAction(
                            _visual_manifest_rejection(
                                call,
                                exc,
                                original=originals.get("forgeai.smoke.json"),
                                root=root,
                            )
                        )
                    if checkpoint is not None:
                        violation = smoke_probe_violation(checkpoint, manifest)
                        if violation is not None:
                            raise ConflictException(violation)
                if not updates and call.name in {
                    "write_files",
                    "write_file",
                    "edit_file_by_replace",
                }:
                    result = result.model_copy(
                        update={
                            "summary": (
                                "提交的文件内容与当前源码相同，没有产生修改；"
                                "请继续检查或实现缺失能力"
                            ),
                            "data": {**result.data, "unchanged_files": paths},
                        }
                    )
            return PreparedAction(result, originals, updates)
    except (AppException, ValueError, OSError, UnicodeError, TypeError) as exc:
        return PreparedAction(rejected_tool(call, exc))


def _replace(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.forgeai-{uuid4().hex}")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def commit_action(action: PreparedAction, root: Path, *, revision_id: str) -> ToolExecutionResult:
    """Caller holds the current execution lock. Never repeat model/tool execution here."""
    if not action.result.ok or (
        not action.updates and not action.deletions and action.input_source_hash is None
    ):
        return action.result
    applied: list[str] = []
    try:
        if "forgeai.smoke.json" in action.deletions:
            raise ConflictException("forgeai.smoke.json 是必需的工程联调门禁，不能删除")
        if action.input_source_hash is not None:
            from app.tools.checks import source_snapshot

            if source_snapshot(root)[1] != action.input_source_hash:
                raise ConflictException("接口生成期间源码发生变化，请重新生成")
        for path, original in action.originals.items():
            target = safe_path_under_root(root, writable_path(root, path), allow_create=True)
            actual = target.read_bytes() if target.exists() else None
            if actual != original:
                raise ConflictException(f"{path} 已变化，请重新读取后修改")
        for path in action.updates.keys() | action.deletions:
            files.snapshot_workspace_source(root, revision_id=revision_id, path=path)
        for path, payload in action.updates.items():
            _replace(safe_path_under_root(root, path, allow_create=True), payload)
            applied.append(path)
        for path in action.deletions:
            safe_path_under_root(root, writable_path(root, path)).unlink()
            applied.append(path)
    except (AppException, OSError) as exc:
        for path in reversed(applied):
            original = action.originals[path]
            target = safe_path_under_root(root, path, allow_create=True)
            if original is None:
                target.unlink(missing_ok=True)
            else:
                _replace(target, original)
        return action.result.model_copy(
            update={
                "ok": False,
                "error_code": "WRITE_CONFLICT",
                "summary": str(exc),
            }
        )
    return action.result.model_copy(
        update={
            "data": {
                **action.result.data,
                "files": [
                    {"path": path, "content_hash": sha256_bytes(payload)}
                    for path, payload in action.updates.items()
                ]
                + [
                    {"path": path, "content_hash": None, "deleted": True}
                    for path in sorted(action.deletions)
                ],
            }
        }
    )
