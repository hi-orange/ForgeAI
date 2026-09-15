"""Pinned generated-app template registry."""

from __future__ import annotations

import json
from pathlib import Path

FULLSTACK_V1 = "fullstack-v1"
DEFAULT_TEMPLATE_VERSION = FULLSTACK_V1

BACKEND_DIR = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = BACKEND_DIR / "templates"


class TemplateNotFoundError(FileNotFoundError):
    """Raised when a requested template version is missing or incomplete."""


def get_template_root(template_version: str = DEFAULT_TEMPLATE_VERSION) -> Path:
    """Return the absolute root of a pinned template version."""
    if not template_version or "/" in template_version or "\\" in template_version:
        raise ValueError(f"invalid template version: {template_version!r}")
    root = (TEMPLATES_DIR / template_version).resolve()
    if not root.is_dir() or not str(root).startswith(str(TEMPLATES_DIR.resolve())):
        raise TemplateNotFoundError(f"template not found: {template_version}")
    required = (
        root / "template.json",
        root / "README.template.md",
        root / "frontend" / "package.json",
        root / "backend" / "app" / "main.py",
        root / "backend" / "alembic" / "versions" / "0001_create_app_meta.py",
    )
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise TemplateNotFoundError(
            f"template {template_version} is incomplete; missing: {', '.join(missing)}"
        )
    return root


def load_template_metadata(template_version: str = DEFAULT_TEMPLATE_VERSION) -> dict:
    """Load template.json for the pinned template."""
    root = get_template_root(template_version)
    return json.loads((root / "template.json").read_text(encoding="utf-8"))
