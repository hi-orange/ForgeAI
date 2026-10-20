"""Generate API artifacts in isolation; the engineering fence owns their commit."""

from __future__ import annotations

import base64
import binascii
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from uuid import uuid4

from app.core.exceptions import BusinessException
from app.core.settings import settings
from app.runtime.docker import get_docker_sandbox_runtime
from app.schemas.agent_action import ToolCall, ToolExecutionResult
from app.tools import checks
from app.tools.paths import MAX_FILE_BYTES

if TYPE_CHECKING:
    from app.generation.engineering.actions import PreparedAction

MARKER = "FORGEAI_API_CLIENT="
MAX_RESULT_BYTES = 2 * 1024 * 1024


def read_generated_files(output: str) -> dict[str, bytes]:
    encoded = next(
        (s[len(MARKER) :] for s in reversed(output.splitlines()) if s.startswith(MARKER)), None
    )
    if encoded is None or len(encoded) > MAX_RESULT_BYTES * 2:
        raise BusinessException("API 生成结果缺失或超过上限")
    try:
        raw = base64.b64decode(encoded, validate=True)
        files = json.loads(raw)
    except (ValueError, binascii.Error, UnicodeError) as exc:
        raise BusinessException("API 生成结果格式无效") from exc
    if len(raw) > MAX_RESULT_BYTES or not isinstance(files, dict) or not 1 <= len(files) <= 128:
        raise BusinessException("API 生成结果超过上限")
    required = {
        "frontend/openapi.json",
        "frontend/src/client/types.gen.ts",
        "frontend/src/client/sdk.gen.ts",
    }
    if not required <= files.keys():
        raise BusinessException("API 生成结果缺少 OpenAPI、类型或 SDK")
    updates = {}
    seen = set()
    for path, content in files.items():
        relative = PurePosixPath(path)
        if (
            not isinstance(content, str)
            or path.casefold() in seen
            or "\\" in path
            or ".." in relative.parts
            or relative.as_posix() != path
            or not (
                path == "frontend/openapi.json"
                or (path.startswith("frontend/src/client/") and relative.suffix == ".ts")
            )
        ):
            raise BusinessException("API 生成结果包含未授权文件")
        seen.add(path.casefold())
        payload = content.encode("utf-8")
        if len(payload) > MAX_FILE_BYTES:
            raise BusinessException(f"API 生成文件超过工作区上限：{path}")
        updates[path] = payload
    return updates


def prepare_client(call: ToolCall, root: Path) -> PreparedAction:
    # Local import avoids a cycle: actions routes this tool and owns PreparedAction.
    from app.generation.engineering.actions import PreparedAction
    from app.generation.engineering.checks import source_file_hashes

    runtime = get_docker_sandbox_runtime()
    binary = runtime.require_image(
        settings.engineering_check_image,
        label=checks.CHECK_RUNTIME_LABEL,
        version=checks.CHECK_RUNTIME_VERSION,
        purpose="API 客户端生成",
    )
    payload, source_hash = checks.source_snapshot(root)
    source = json.loads(payload)
    name = f"forgeai-api-client-{uuid4().hex}"
    command = checks.docker_command(binary, settings.engineering_check_image, name, "quick")
    command[-2:] = ["/opt/forgeai/generate_client.py"]
    try:
        code, output, _ = checks._execute(command, payload, 240, max_output_bytes=4 * 1024 * 1024)
        if code != 0:
            raise BusinessException(f"API 客户端生成失败：{output[-3000:]}")
        updates = read_generated_files(output)
    except subprocess.TimeoutExpired as exc:
        raise BusinessException("API 客户端生成超时") from exc
    finally:
        runtime.remove_container(binary, name)
    obsolete = {
        path for path in source if path.startswith("frontend/src/client/") and path not in updates
    }
    originals = {}
    for path in updates.keys() | obsolete:
        previous = source.get(path)
        if previous is not None and not isinstance(previous, str):
            raise BusinessException("API 生成目标不是文本文件")
        originals[path] = previous.encode("utf-8") if previous is not None else None
    changed = {path: value for path, value in updates.items() if originals[path] != value}
    result = ToolExecutionResult(
        tool_call_id=call.id,
        name=call.name,
        arguments=call.arguments,
        ok=True,
        summary="已从实际 FastAPI 导出 OpenAPI 并生成类型和 SDK；仍需类型检查与业务验收",
        data={
            "backend_files": {
                p: h for p, h in source_file_hashes(payload).items() if p.startswith("backend/")
            },
            "generated_files": source_file_hashes(
                json.dumps(
                    {p: raw.decode("utf-8") for p, raw in updates.items()}, ensure_ascii=False
                ).encode("utf-8")
            ),
            "generated_paths": sorted(updates),
            "runtime_version": checks.CHECK_RUNTIME_VERSION,
        },
    )
    return PreparedAction(
        result, originals, changed, deletions=obsolete, input_source_hash=source_hash
    )
