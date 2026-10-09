"""Path-safe image generation for generated application workspaces."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from app.core.exceptions import BusinessException
from app.core.image_generation import generate_image
from app.schemas.agent_action import ToolExecutionResult
from app.tools.paths import safe_path_under_root, sha256_bytes

_MEDIA_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def generate_project_image(
    root: Path,
    *,
    path: str,
    prompt: str,
    size: str = "2K",
    watermark: bool = True,
    tool_call_id: str = "generate_image",
    before_write: Callable[[str], None] | None = None,
) -> ToolExecutionResult:
    normalized = path.replace("\\", "/").strip()
    if not normalized.startswith("frontend/public/"):
        raise BusinessException("生成图片必须写入 frontend/public/ 下的本地资产目录")
    target = safe_path_under_root(root, normalized, allow_create=True)
    result = generate_image(prompt, size=size, watermark=watermark)
    expected_suffix = _MEDIA_SUFFIXES[result.media_type]
    actual_suffix = ".jpg" if target.suffix.lower() == ".jpeg" else target.suffix.lower()
    if actual_suffix != expected_suffix:
        target = target.with_suffix(expected_suffix)
        normalized = target.relative_to(root.resolve()).as_posix()
    if before_write is not None:
        before_write(normalized)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{tool_call_id}.tmp")
    try:
        temporary.write_bytes(result.data)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    content_hash = sha256_bytes(result.data)
    return ToolExecutionResult(
        tool_call_id=tool_call_id,
        name="generate_image",
        ok=True,
        summary=f"已生成本地图片 {normalized}",
        data={
            "path": normalized,
            "content_hash": content_hash,
            "media_type": result.media_type,
            "bytes": len(result.data),
        },
    )
