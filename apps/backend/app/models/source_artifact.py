from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class SourceArtifact(Base):
    """Immutable metadata for one content-addressed generated source snapshot."""

    __tablename__ = "source_artifact"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_source_artifact_size_nonnegative"),
        UniqueConstraint(
            "project_id",
            "source_hash",
            name="uq_source_artifact_project_hash",
        ),
    )

    artifact_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_format: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )


@event.listens_for(SourceArtifact, "before_update")
def _reject_source_artifact_update(*_args: object) -> None:
    raise ValueError("SourceArtifact 是不可变源码事实，不允许修改")


@event.listens_for(SourceArtifact, "before_delete")
def _reject_source_artifact_delete(*_args: object) -> None:
    raise ValueError("SourceArtifact 是不可变源码事实，不允许删除")
