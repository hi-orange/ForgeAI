"""Recoverable pre-write snapshots for generated workspace files."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.core.exceptions import ConflictException
from app.tools.files import restore_workspace_revision, snapshot_workspace_source


def test_revision_restores_changed_and_new_files() -> None:
    with TemporaryDirectory(prefix="forgeai-revision-") as directory:
        root = Path(directory)
        existing = root / "frontend/src/App.tsx"
        existing.parent.mkdir(parents=True)
        existing.write_text("before", encoding="utf-8")

        snapshot_workspace_source(root, revision_id="exec_123", path="frontend/src/App.tsx")
        snapshot_workspace_source(root, revision_id="exec_123", path="frontend/src/new.ts")
        existing.write_text("after", encoding="utf-8")
        created = root / "frontend/src/new.ts"
        created.write_text("new", encoding="utf-8")

        restored = restore_workspace_revision(root, revision_id="exec_123")

        assert restored == ["frontend/src/App.tsx", "frontend/src/new.ts"]
        assert existing.read_text(encoding="utf-8") == "before"
        assert not created.exists()


def test_revision_keeps_first_snapshot_for_repeated_writes() -> None:
    with TemporaryDirectory(prefix="forgeai-revision-") as directory:
        root = Path(directory)
        target = root / "backend/app/main.py"
        target.parent.mkdir(parents=True)
        target.write_text("v1", encoding="utf-8")

        snapshot_workspace_source(root, revision_id="exec_repeat", path="backend/app/main.py")
        target.write_text("v2", encoding="utf-8")
        snapshot_workspace_source(root, revision_id="exec_repeat", path="backend/app/main.py")
        target.write_text("v3", encoding="utf-8")

        restore_workspace_revision(root, revision_id="exec_repeat")
        assert target.read_text(encoding="utf-8") == "v1"


def test_revision_rejects_unsafe_identity_and_platform_path() -> None:
    with TemporaryDirectory(prefix="forgeai-revision-") as directory:
        root = Path(directory)
        with pytest.raises(ConflictException):
            snapshot_workspace_source(root, revision_id="../escape", path="frontend/src/App.tsx")
        with pytest.raises(ConflictException):
            snapshot_workspace_source(root, revision_id="exec_safe", path="forgeai/state.json")
