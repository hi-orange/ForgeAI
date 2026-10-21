"""Versioned optional source bundles, selected only by a frozen approved design."""

from __future__ import annotations

import json
from typing import Any

from app.generation.template_registry import (
    BACKEND_DIR,
    DEFAULT_TEMPLATE_VERSION,
    get_template_root,
)
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.tools.paths import sha256_bytes

CAPABILITY_ROOT = BACKEND_DIR / "capabilities"
AUTH_SESSION_ID = "auth-session-v1"
_AUTH_FILES = (
    "backend/app/core/auth.py",
    "backend/app/models/auth.py",
    "backend/app/api/auth.py",
    "backend/alembic/versions/0002_auth_session.py",
    "frontend/src/lib/auth.tsx",
    "docs/auth-session.md",
)
_ENTRY_PATHS = ("backend/app/api/router.py", "backend/app/models/__init__.py")


def capability_catalog() -> list[dict[str, Any]]:
    """Only implemented source bundles are advertised; availability is not activation."""
    return [
        {
            "capability_id": AUTH_SESSION_ID,
            "description": (
                "可选身份基础：用户名/密码登录、可撤销会话、退出、当前身份、迁移和 React 状态"
            ),
            "template_version": DEFAULT_TEMPLATE_VERSION,
            "requires_approved_features": True,
            "requires_read": list(_ENTRY_PATHS),
            "limits": (
                "仅首次安装到未修改的模板入口/迁移链；无默认账户、公开注册或邮件。"
                "用户开通、页面及业务所有权按获批需求实现；刷新后需重新登录。"
            ),
        }
    ]


def capability_sources(capability_id: str, template_version: str) -> dict[str, bytes]:
    if capability_id != AUTH_SESSION_ID or template_version != DEFAULT_TEMPLATE_VERSION:
        raise ValueError("能力包或模板版本不受支持")
    root = CAPABILITY_ROOT / capability_id
    result = {path: (root / path).read_bytes() for path in _AUTH_FILES}
    template = get_template_root(template_version)
    router = (template / _ENTRY_PATHS[0]).read_bytes()
    result[_ENTRY_PATHS[0]] = (
        router.replace(b"from app.api import health", b"from app.api import auth, health").rstrip()
        + b"\napi_router.include_router(auth.router)\n"
    )
    models = (template / _ENTRY_PATHS[1]).read_bytes()
    result[_ENTRY_PATHS[1]] = (
        b"from app.models.auth import AuthSession, AuthUser\n"
        + models.replace(b'["AppMeta"]', b'["AppMeta", "AuthSession", "AuthUser"]')
    )
    return result


def capability_digest(capability_id: str, template_version: str) -> str:
    return capability_source_digest(capability_sources(capability_id, template_version))


def capability_source_digest(sources: dict[str, bytes]) -> str:
    manifest = {path: sha256_bytes(payload) for path, payload in sorted(sources.items())}
    return sha256_bytes(json.dumps(manifest, sort_keys=True).encode())


def freeze_optional_capabilities(
    spec: AppSpec,
    design: SystemDesign | None,
    template_version: str = DEFAULT_TEMPLATE_VERSION,
) -> list[dict[str, Any]]:
    if design is None:
        return []
    design.validate_requirement_dependencies(spec)
    catalog = {entry["capability_id"]: entry for entry in capability_catalog()}
    return [
        {
            **catalog[selection.capability_id],
            **selection.model_dump(mode="json"),
            "source_digest": capability_digest(selection.capability_id, template_version),
        }
        for selection in design.optional_capabilities
    ]


def capability_entry_paths() -> tuple[str, ...]:
    return _ENTRY_PATHS


def baseline_migrations(template_version: str) -> dict[str, bytes]:
    directory = get_template_root(template_version) / "backend/alembic/versions"
    return {path.name: path.read_bytes() for path in directory.glob("*.py")}
