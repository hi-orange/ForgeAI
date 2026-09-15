"""Copy a pinned fullstack template into an isolated engineering workspace."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.settings import settings
from app.generation.template_registry import DEFAULT_TEMPLATE_VERSION, get_template_root

# Skip local/runtime junk if ever present under the template tree.
_IGNORE = shutil.ignore_patterns(
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "*.py[cod]",
    ".env",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "dist",
    ".turbo",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
)


class WorkspaceExistsError(FileExistsError):
    """Raised when create_workspace would overwrite an existing directory."""


def create_workspace(
    destination: str | Path,
    *,
    template_version: str = DEFAULT_TEMPLATE_VERSION,
) -> Path:
    """
    Copy ``templates/<version>`` into a new independent directory for one task.

    The destination must not already exist. Returns the resolved workspace path.
    Business features are not part of the template; generators add them later.
    """
    source = get_template_root(template_version)
    dest = Path(destination).expanduser().resolve()
    if dest.exists():
        raise WorkspaceExistsError(f"workspace already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    shutil.copytree(source, dest, ignore=_IGNORE, symlinks=False)

    readme_template = dest / "README.template.md"
    readme = dest / "README.md"
    if readme_template.is_file() and not readme.exists():
        shutil.copy2(readme_template, readme)

    return dest


def default_workspace_path(
    runtime_data_root: str | Path,
    project_id: int,
    execution_id: str,
) -> Path:
    """Build ``runtime-data/work/{project_id}/{execution_id}`` without creating it."""
    if project_id <= 0:
        raise ValueError("project_id must be positive")
    if not execution_id or "/" in execution_id or "\\" in execution_id or ".." in execution_id:
        raise ValueError(f"invalid execution_id: {execution_id!r}")
    return Path(runtime_data_root).expanduser().resolve() / "work" / str(project_id) / execution_id


def prepare_engineering_workspace(
    project_id: int,
    run_id: str,
    *,
    task_id: str,
    approved_item_id: str,
    app_spec: dict[str, Any] | None = None,
    template_version: str = DEFAULT_TEMPLATE_VERSION,
    runtime_data_root: str | Path | None = None,
) -> Path:
    """
    After requirements approval, materialize the fullstack template for this run.

    Idempotent and safe under concurrent handoff retries: copies into a staging
    directory, then renames into place. Marker + approved spec are refreshed.
    Until an execution_id exists, the work directory key is the BuildRun ``run_id``.
    """
    root = runtime_data_root if runtime_data_root is not None else settings.runtime_data_root
    dest = default_workspace_path(root, project_id, run_id)

    if not _workspace_tree_ready(dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = dest.parent / f".{dest.name}.creating-{uuid4().hex}"
        try:
            create_workspace(staging, template_version=template_version)
            try:
                staging.rename(dest)
            except OSError:
                # Another worker materialized the same run workspace first.
                shutil.rmtree(staging, ignore_errors=True)
                if not _workspace_tree_ready(dest):
                    raise WorkspaceExistsError(
                        f"workspace path exists but is not a valid template copy: {dest}"
                    ) from None
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    marker_dir = dest / "forgeai"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = {
        "template_version": template_version,
        "project_id": project_id,
        "run_id": run_id,
        "task_id": task_id,
        "approved_item_id": approved_item_id,
    }
    (marker_dir / "workspace.json").write_text(
        json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if app_spec is not None:
        (marker_dir / "approved_app_spec.json").write_text(
            json.dumps(app_spec, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return dest


def _workspace_tree_ready(dest: Path) -> bool:
    return (dest / "backend" / "app" / "main.py").is_file() and (
        dest / "frontend" / "package.json"
    ).is_file()


def workspace_is_ready(
    project_id: int,
    run_id: str,
    *,
    runtime_data_root: str | Path | None = None,
) -> bool:
    """True when the run already has a materialized fullstack workspace."""
    root = runtime_data_root if runtime_data_root is not None else settings.runtime_data_root
    return _workspace_tree_ready(default_workspace_path(root, project_id, run_id))
