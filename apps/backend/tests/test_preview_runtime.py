"""Docker preview runtime security boundary and lifecycle tests."""

from __future__ import annotations

import os
import socket
import tempfile
import time
import unittest
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.exceptions import BusinessException
from app.generation.workspace import create_workspace
from app.runtime.preview import (
    DockerPreviewRuntime,
    PreviewSandboxHandle,
    PreviewSandboxRequest,
    preview_docker_command,
)


class PreviewRuntimeTests(unittest.TestCase):
    def _request(self, root: Path) -> PreviewSandboxRequest:
        return PreviewSandboxRequest(
            project_id=17,
            run_id="run_abc",
            workspace=root,
            bind_host="127.0.0.1",
            host_port=18201,
            log_path=root / "forgeai/logs/preview-sandbox.log",
        )

    def test_container_has_no_host_mount_socket_or_platform_secrets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgeai-runtime-") as tmp:
            command = preview_docker_command(
                "docker",
                "trusted-image",
                self._request(Path(tmp)),
                "forgeai-preview-owned",
            )

        for flag in (
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--pull=never",
            "--init",
        ):
            self.assertIn(flag, command)
        self.assertIn("127.0.0.1:18201:8080", command)
        self.assertIn("--pids-limit=256", command)
        self.assertNotIn("--volume", command)
        self.assertNotIn("--mount", command)
        self.assertFalse(any("docker.sock" in item for item in command))
        self.assertFalse(any("DEEPSEEK" in item or "SECRET_KEY" in item for item in command))
        self.assertEqual(command[-2:], ["-I", "/opt/forgeai/preview.py"])

    def test_container_rejects_non_loopback_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgeai-runtime-") as tmp:
            request = self._request(Path(tmp))
            request = replace(request, bind_host="0.0.0.0")
            with self.assertRaisesRegex(BusinessException, "127.0.0.1"):
                preview_docker_command("docker", "trusted-image", request, "preview")

    def test_start_streams_bounded_source_snapshot_over_stdin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgeai-runtime-") as tmp:
            root = Path(tmp)
            request = self._request(root)
            stdin = MagicMock()
            process = MagicMock()
            process.stdin = stdin
            process.poll.return_value = None
            runtime = DockerPreviewRuntime()
            docker = MagicMock()
            docker.start_streaming.return_value = process
            with (
                patch.object(runtime, "_binary_and_image", return_value=("docker", "image")),
                patch("app.runtime.preview.get_docker_sandbox_runtime", return_value=docker),
                patch(
                    "app.runtime.preview.source_snapshot",
                    return_value=(b'{"safe":"source"}', "a" * 64),
                ),
            ):
                handle = runtime.start(request)

        self.assertEqual(handle.source_hash, "a" * 64)
        self.assertEqual(docker.start_streaming.call_args.kwargs["payload"], b'{"safe":"source"}')
        command = docker.start_streaming.call_args.args[0]
        self.assertNotIn(str(root), command)
        self.assertIn("/opt/forgeai/preview.py", command)
        docker.remove_container.assert_called_once()

    def test_runtime_preflight_requires_current_preview_image(self) -> None:
        runtime = DockerPreviewRuntime()
        docker = MagicMock()
        docker.require_image.side_effect = BusinessException("隔离预览镜像版本过旧")
        with (
            patch("app.runtime.preview.get_docker_sandbox_runtime", return_value=docker),
            self.assertRaisesRegex(BusinessException, "版本过旧"),
        ):
            runtime._binary_and_image()

    def test_stop_removes_only_the_owned_container(self) -> None:
        runtime = DockerPreviewRuntime()
        handle = PreviewSandboxHandle(
            sandbox_id="forgeai-preview-owned",
            source_hash="a" * 64,
            host="127.0.0.1",
            port=18201,
            log_path=Path("preview.log"),
        )
        process = MagicMock()
        process.poll.return_value = 0
        runtime._processes[handle.sandbox_id] = process
        docker = MagicMock()
        docker.require_daemon.return_value = "docker"
        with patch("app.runtime.preview.get_docker_sandbox_runtime", return_value=docker):
            runtime.stop(handle)

        docker.remove_container.assert_called_once_with("docker", "forgeai-preview-owned")
        self.assertNotIn(handle.sandbox_id, runtime._processes)

    @unittest.skipUnless(
        os.getenv("FORGEAI_TEST_DOCKER") == "1", "requires built preview image and Docker"
    )
    def test_real_preview_serves_frontend_and_backend_from_container(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgeai-preview-integration-") as directory:
            root = Path(directory) / "app"
            create_workspace(root)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = int(probe.getsockname()[1])
            request = PreviewSandboxRequest(
                project_id=17,
                run_id="integration",
                workspace=root,
                bind_host="127.0.0.1",
                host_port=port,
                log_path=root / "forgeai/logs/preview-sandbox.log",
            )
            runtime = DockerPreviewRuntime()
            handle = runtime.start(request)
            try:
                deadline = time.monotonic() + 180
                last_error = "preview did not answer"
                while time.monotonic() < deadline:
                    if not runtime.is_running(handle):
                        self.fail(runtime.diagnostic_tail(handle) or "preview container exited")
                    try:
                        with urllib.request.urlopen(
                            f"{handle.base_url}/api/v1/health", timeout=3
                        ) as response:
                            health = response.read()
                        with urllib.request.urlopen(handle.base_url, timeout=3) as response:
                            html = response.read()
                            status = response.status
                        self.assertEqual(status, 200)
                        self.assertIn(b"ok", health.lower())
                        self.assertIn(b'<div id="root"></div>', html)
                        break
                    except OSError as exc:
                        last_error = str(exc)
                        time.sleep(1)
                else:
                    self.fail(f"{last_error}\n{runtime.diagnostic_tail(handle)}")
            finally:
                runtime.stop(handle)
            self.assertFalse(runtime.is_running(handle))


if __name__ == "__main__":
    unittest.main()
