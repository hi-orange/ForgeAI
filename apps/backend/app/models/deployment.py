from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class Deployment(Base):
    """A durable publication attempt for one exact, accepted BuildRun."""

    __tablename__ = "deployment"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'building', 'ready', 'failed', 'stopped', 'rolled_back')",
            name="ck_deployment_status",
        ),
        CheckConstraint("revision > 0", name="ck_deployment_positive_revision"),
    )

    deployment_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    build_run_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("build_run.run_id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="local_docker")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    url: Mapped[str | None] = mapped_column(String(500))
    domain: Mapped[str | None] = mapped_column(String(255))
    image_reference: Mapped[str | None] = mapped_column(String(500))
    secret_reference: Mapped[str | None] = mapped_column(String(200))
    compose_project: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    port: Mapped[int | None] = mapped_column(Integer)
    previous_deployment_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("deployment.deployment_id", ondelete="SET NULL")
    )
    logs: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime)
