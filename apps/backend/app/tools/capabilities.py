"""Stage an authorized capability bundle through the normal fenced source commit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.exceptions import AppException, ConflictException
from app.generation.capabilities import (
    baseline_migrations,
    capability_entry_paths,
    capability_source_digest,
    capability_sources,
)
from app.generation.engineering.actions import PreparedAction, writable_path
from app.generation.template_registry import DEFAULT_TEMPLATE_VERSION, get_template_root
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.tools.checks import source_snapshot
from app.tools.code_engineer import rejected_tool, validate_tool_call
from app.tools.paths import safe_path_under_root, sha256_bytes


def prepare_capability(
    call: ToolCall,
    root: Path,
    observed_hashes: dict[str, str],
    *,
    spec: AppSpec,
    system_design: SystemDesign | None,
    capability_snapshot: list[dict[str, Any]] | None,
) -> PreparedAction:
    try:
        call = validate_tool_call(call)
        if call.name != "install_capability" or system_design is None:
            raise ConflictException("仅可安装冻结系统设计明确选择的能力")
        system_design.validate_requirement_dependencies(spec)
        capability_id = call.arguments["capability_id"]
        selection = next(
            (
                row
                for row in system_design.optional_capabilities
                if row.capability_id == capability_id
            ),
            None,
        )
        frozen = next(
            (row for row in capability_snapshot or [] if row.get("capability_id") == capability_id),
            None,
        )
        if selection is None or frozen is None:
            raise ConflictException("此能力未在本次冻结输入中选中，不能按名称猜测需求")
        if (
            frozen.get("requirement_ids") != selection.requirement_ids
            or frozen.get("rationale") != selection.rationale
            or frozen.get("template_version") != DEFAULT_TEMPLATE_VERSION
        ):
            raise ConflictException("能力选择与冻结设计不一致")
        template_version = frozen["template_version"]
        sources = capability_sources(capability_id, template_version)
        if frozen.get("source_digest") != capability_source_digest(sources):
            raise ConflictException("能力包内容与冻结版本不同，不能静默升级")
        input_source_hash = source_snapshot(root)[1]
        template = get_template_root(template_version)
        entry_paths = capability_entry_paths()
        originals: dict[str, bytes | None] = {}
        for path in sources:
            canonical = writable_path(root, path)
            target = safe_path_under_root(root, canonical, allow_create=True)
            original = target.read_bytes() if target.exists() else None
            if path in entry_paths:
                if original is None or original != (template / path).read_bytes():
                    raise ConflictException(f"{path} 已有自定义代码，请按冻结能力契约局部集成")
                if observed_hashes.get(path) != sha256_bytes(original):
                    raise ConflictException(f"安装前必须读取 {path} 获取当前源码版本")
            elif original is not None:
                raise ConflictException(f"能力目标 {path} 已存在，不能重复安装或覆盖业务代码")
            originals[path] = original
        migration_dir = safe_path_under_root(root, "backend/alembic/env.py").parent / "versions"
        actual = {
            path.name: safe_path_under_root(root, path.relative_to(root).as_posix()).read_bytes()
            for path in migration_dir.glob("*.py")
        }
        if actual != baseline_migrations(template_version):
            raise ConflictException("已有自定义迁移链，请按冻结能力契约编写兼容迁移")
        return PreparedAction(
            result=ToolExecutionResult(
                tool_call_id=call.id,
                name=call.name,
                ok=True,
                summary="身份会话能力已暂存，提交后仍需获批业务集成和验证",
                arguments=call.arguments,
                data={"capability_id": capability_id, "source_digest": frozen["source_digest"]},
            ),
            originals=originals,
            updates=sources,
            input_source_hash=input_source_hash,
        )
    except (AppException, ValueError, OSError) as exc:
        return PreparedAction(rejected_tool(call, exc))
