"""Single trusted Docker boundary for checks, dependencies, previews and deployment."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import IO

from app.core.exceptions import BusinessException

DOCKER_PROBE_TIMEOUT_SECONDS = 30


def creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class DockerSandboxRuntime:
    """Own shared Docker daemon access and fixed platform-controlled invocations."""

    def require_daemon(self, *, purpose: str) -> str:
        binary = shutil.which("docker")
        if binary is None:
            raise BusinessException(f"{purpose}环境未就绪：未找到 Docker")
        try:
            probe = subprocess.run(
                [binary, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=DOCKER_PROBE_TIMEOUT_SECONDS,
                creationflags=creation_flags(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BusinessException(f"{purpose}环境探测失败：{str(exc)[:240]}") from exc
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout).strip()
            raise BusinessException(
                f"{purpose}环境未就绪：Docker daemon 不可用"
                + (f"：{detail[:240]}" if detail else "")
            )
        return binary

    def require_image(
        self,
        image: str,
        *,
        label: str,
        version: str,
        purpose: str,
    ) -> str:
        binary = self.require_daemon(purpose=purpose)
        try:
            inspected = subprocess.run(
                [
                    binary,
                    "image",
                    "inspect",
                    "--format",
                    f'{{{{ index .Config.Labels "{label}" }}}}',
                    image,
                ],
                capture_output=True,
                text=True,
                timeout=DOCKER_PROBE_TIMEOUT_SECONDS,
                creationflags=creation_flags(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BusinessException(f"{purpose}镜像探测失败：{str(exc)[:240]}") from exc
        if inspected.returncode != 0:
            raise BusinessException(f"{purpose}镜像不存在：请重新构建 {image}")
        if inspected.stdout.strip() != version:
            raise BusinessException(f"{purpose}镜像版本过旧：请重新构建 {image}")
        return binary

    @staticmethod
    def isolation_args(
        *,
        cpus: float,
        memory_mb: int,
        pids_limit: int,
        tmpfs: str,
        writable_layer: bool = False,
        qa_controller: bool = False,
    ) -> list[str]:
        return [
            *([] if writable_layer else ["--read-only", f"--tmpfs=/tmp:{tmpfs}"]),
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            *(
                [
                    "--user=0:0",
                    "--network=none",
                    "--cap-add=SETUID",
                    "--cap-add=SETGID",
                    "--cap-add=CHOWN",
                    "--cap-add=KILL",
                ]
                if qa_controller
                else ["--user=65534:65534"]
            ),
            f"--cpus={cpus}",
            f"--memory={memory_mb}m",
            f"--memory-swap={memory_mb}m",
            f"--pids-limit={pids_limit}",
            "--log-driver=none",
            "--init",
        ]

    @staticmethod
    def run_bytes(
        command: Sequence[str],
        *,
        payload: bytes,
        timeout: int,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            list(command),
            input=payload,
            capture_output=True,
            timeout=timeout,
            creationflags=creation_flags(),
            check=False,
        )

    @staticmethod
    def start_streaming(
        command: Sequence[str],
        *,
        payload: bytes,
        output: IO[bytes],
    ) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags(),
        )
        assert process.stdin is not None
        process.stdin.write(payload)
        process.stdin.close()
        return process

    @staticmethod
    def remove_container(binary: str, name: str) -> None:
        try:
            subprocess.run(
                [binary, "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=creation_flags(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    @staticmethod
    def is_running(binary: str, name: str) -> bool:
        inspected = subprocess.run(
            [binary, "inspect", "--format", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=creation_flags(),
            check=False,
        )
        return inspected.returncode == 0 and inspected.stdout.strip().lower() == "true"

    @staticmethod
    def copy_from(
        binary: str, container_path: str, destination: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [binary, "cp", container_path, str(destination)],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=creation_flags(),
            check=False,
        )

    @staticmethod
    def compose(
        binary: str,
        *,
        workspace: Path,
        env_file: Path,
        project: str,
        arguments: Sequence[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [binary, "compose", "--env-file", str(env_file), "-p", project, *arguments],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=creation_flags(),
            check=False,
        )


_docker_runtime = DockerSandboxRuntime()


def get_docker_sandbox_runtime() -> DockerSandboxRuntime:
    return _docker_runtime
