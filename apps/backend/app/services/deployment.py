"""Docker Compose publication for accepted generated applications."""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, ConflictException, NotFoundException
from app.core.settings import settings
from app.db.database import SessionLocal
from app.generation.template_registry import get_template_root
from app.generation.workspace import default_workspace_path, workspace_is_ready
from app.models.deployment import Deployment
from app.models.user import User
from app.services import project as project_service
from app.services.requirements import get_requirements_status

_MAX_LOG_CHARS = 20_000
_start_lock = threading.Lock()
_TRUSTED_DEPLOYMENT_FILES = (
    "compose.yml",
    "backend/Dockerfile",
    "frontend/Dockerfile",
    "frontend/nginx.conf",
)


def _deployment_root(deployment_id: str) -> Path:
    return Path(settings.runtime_data_root).resolve() / "deployments" / deployment_id


def _env_path(deployment_id: str) -> Path:
    return _deployment_root(deployment_id) / ".env"


def _allocate_port() -> int:
    host = settings.deployment_bind_host
    for port in range(settings.deployment_port_min, settings.deployment_port_max + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise BusinessException("没有可用的发布端口")


def _docker_binary() -> str:
    binary = shutil.which("docker")
    if binary is None:
        raise BusinessException("发布环境未安装 Docker")
    probe = subprocess.run([binary, "info"], capture_output=True, timeout=15, check=False)
    if probe.returncode != 0:
        raise BusinessException("Docker 守护进程不可用")
    return binary


def _assert_trusted_deployment_files(workspace: Path) -> None:
    """Do not let generated source redefine the host Docker build boundary."""

    template = get_template_root()
    for relative in _TRUSTED_DEPLOYMENT_FILES:
        candidate = workspace / relative
        trusted = template / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise BusinessException(f"发布基础设施文件缺失或不可信：{relative}")
        try:
            if candidate.read_bytes() != trusted.read_bytes():
                raise BusinessException(f"发布基础设施文件已被修改：{relative}")
        except OSError as exc:
            raise BusinessException(f"无法校验发布基础设施文件：{relative}") from exc


def _write_secret_env(deployment: Deployment) -> Path:
    root = _deployment_root(deployment.deployment_id)
    root.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(32)
    url = f"http://{settings.deployment_bind_host}:{deployment.port}"
    path = root / ".env"
    path.write_text(
        "\n".join(
            [
                "POSTGRES_DB=app",
                "POSTGRES_USER=app",
                f"POSTGRES_PASSWORD={password}",
                f"DATABASE_URL=postgresql+psycopg://app:{password}@db:5432/app",
                f"APP_PORT={deployment.port}",
                f"CORS_ORIGINS={url}",
                "APP_NAME=Generated App",
                "",
            ]
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _run_compose(
    deployment: Deployment, *arguments: str, timeout: int | None = None
) -> subprocess.CompletedProcess[str]:
    workspace = default_workspace_path(
        settings.runtime_data_root, deployment.project_id, deployment.build_run_id
    )
    binary = _docker_binary()
    return subprocess.run(
        [
            binary,
            "compose",
            "--env-file",
            str(_env_path(deployment.deployment_id)),
            "-p",
            deployment.compose_project,
            *arguments,
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout or settings.deployment_build_timeout_seconds,
        check=False,
    )


def _bounded_logs(completed: subprocess.CompletedProcess[str]) -> str:
    return (completed.stdout + "\n" + completed.stderr).strip()[-_MAX_LOG_CHARS:]


def _deploy_worker(deployment_id: str) -> None:
    with SessionLocal() as db:
        deployment = db.get(Deployment, deployment_id)
        if deployment is None:
            return
        deployment.status = "building"
        db.commit()
        try:
            result = _run_compose(deployment, "up", "--build", "-d", "--wait")
            logs_result = _run_compose(deployment, "logs", "--no-color", "--tail", "200")
            deployment.logs = _bounded_logs(logs_result)
            if result.returncode != 0:
                raise BusinessException(_bounded_logs(result) or "Docker Compose 发布失败")
            deployment.status = "ready"
            deployment.url = f"http://{settings.deployment_bind_host}:{deployment.port}"
            deployment.domain = settings.deployment_bind_host
            deployment.image_reference = (
                f"{deployment.compose_project}-backend,{deployment.compose_project}-frontend"
            )
            deployment.deployed_at = datetime.now(UTC).replace(tzinfo=None)
            deployment.error = None
            db.commit()
        except Exception as exc:  # noqa: BLE001 - persisted worker boundary
            db.rollback()
            deployment = db.get(Deployment, deployment_id)
            if deployment is not None:
                deployment.status = "failed"
                deployment.error = (str(exc).strip() or exc.__class__.__name__)[-_MAX_LOG_CHARS:]
                db.commit()


def start_deployment(db: Session, user: User, project_id: int) -> Deployment:
    project_service.get_user_project(db, user, project_id)
    if not settings.deployment_enabled:
        raise BusinessException("发布能力未启用")
    status = get_requirements_status(db, user, project_id)
    if status.state != "completed" or status.run_id is None:
        raise BusinessException("只有独立验收通过的版本才能发布")
    if not workspace_is_ready(project_id, status.run_id):
        raise BusinessException("发布工作区不存在")
    workspace = default_workspace_path(settings.runtime_data_root, project_id, status.run_id)
    _assert_trusted_deployment_files(workspace)
    _docker_binary()
    with _start_lock:
        active = db.scalar(
            select(Deployment)
            .where(
                Deployment.project_id == project_id,
                Deployment.status.in_(("queued", "building", "ready")),
            )
            .order_by(Deployment.revision.desc())
            .limit(1)
        )
        if active is not None and active.build_run_id == status.run_id:
            return active
        revision = (
            int(
                db.scalar(
                    select(func.max(Deployment.revision)).where(Deployment.project_id == project_id)
                )
                or 0
            )
            + 1
        )
        deployment_id = f"dep_{uuid4().hex}"
        deployment = Deployment(
            deployment_id=deployment_id,
            project_id=project_id,
            build_run_id=status.run_id,
            revision=revision,
            provider="local_docker",
            status="queued",
            compose_project=f"forgeai-{project_id}-{deployment_id[-8:]}",
            port=_allocate_port(),
            previous_deployment_id=active.deployment_id if active else None,
            secret_reference=f"local-file:{deployment_id}",
        )
        db.add(deployment)
        db.commit()
        db.refresh(deployment)
        _write_secret_env(deployment)
        threading.Thread(
            target=_deploy_worker,
            args=(deployment.deployment_id,),
            name=f"forgeai-deploy-{deployment.deployment_id[-8:]}",
            daemon=True,
        ).start()
        return deployment


def list_deployments(db: Session, user: User, project_id: int) -> list[Deployment]:
    project_service.get_user_project(db, user, project_id)
    return list(
        db.scalars(
            select(Deployment)
            .where(Deployment.project_id == project_id)
            .order_by(Deployment.revision.desc())
        ).all()
    )


def get_deployment(db: Session, user: User, project_id: int, deployment_id: str) -> Deployment:
    project_service.get_user_project(db, user, project_id)
    deployment = db.scalar(
        select(Deployment).where(
            Deployment.project_id == project_id,
            Deployment.deployment_id == deployment_id,
        )
    )
    if deployment is None:
        raise NotFoundException("发布记录不存在")
    return deployment


def rollback_deployment(db: Session, user: User, project_id: int, deployment_id: str) -> Deployment:
    current = get_deployment(db, user, project_id, deployment_id)
    if current.status != "ready" or not current.previous_deployment_id:
        raise ConflictException("当前发布没有可回滚的上一版本")
    previous = get_deployment(db, user, project_id, current.previous_deployment_id)
    if previous.status not in {"ready", "stopped"}:
        raise ConflictException("上一发布版本不可恢复")
    stopped = _run_compose(current, "stop", timeout=60)
    if stopped.returncode != 0:
        raise BusinessException(_bounded_logs(stopped) or "停止当前发布失败")
    if previous.status == "stopped":
        started = _run_compose(previous, "start", timeout=120)
        if started.returncode != 0:
            raise BusinessException(_bounded_logs(started) or "恢复上一发布失败")
    current.status = "rolled_back"
    previous.status = "ready"
    db.commit()
    db.refresh(previous)
    return previous
