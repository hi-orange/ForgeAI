"""Stage tool side effects, then commit only inside the execution fence."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.core.exceptions import AppException, ConflictException
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.tools import files
from app.tools.code_engineer import (
    MUTATING_TOOLS,
    execute_tool_call,
    rejected_tool,
    validate_tool_call,
)
from app.tools.paths import safe_path_under_root, sha256_bytes
from sandbox.smoke_manifest import parse_smoke_manifest

PROTECTED_ACCEPTANCE_PATHS = (
    "backend/tests/acceptance/",
    "frontend/src/acceptance/",
    "frontend/e2e/",
    "forgeai.acceptance.json",
)


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


def prepare_action(call: ToolCall, root: Path, observed_hashes: dict[str, str]) -> PreparedAction:
    """Network/model/container work happens here, outside database transactions."""
    try:
        call = validate_tool_call(call)
        if call.name not in MUTATING_TOOLS:
            return PreparedAction(execute_tool_call(call, workspace_root=root))
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
                    "修改已有源码前必须 read_file 并使用该次读取的 content_hash"
                )
        if writes is not None:
            for row in writes:
                if originals[row["path"]] is not None and (
                    not row.get("expected_hash")
                    or observed_hashes.get(row["path"]) != row["expected_hash"]
                ):
                    raise ConflictException(f"修改 {row['path']} 前必须读取并使用 content_hash")
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
                    metadata = updates.get("template.json")
                    if metadata is None:
                        metadata = safe_path_under_root(root, "template.json").read_bytes()
                    parse_smoke_manifest(
                        updates["forgeai.smoke.json"].decode("utf-8"),
                        root=root.resolve(),
                        template_metadata=json.loads(metadata),
                    )
            return PreparedAction(result, originals, updates)
    except (AppException, ValueError, OSError) as exc:
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
