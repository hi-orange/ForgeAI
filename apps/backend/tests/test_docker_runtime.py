"""The common Docker runtime owns daemon, image and Compose invocation."""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.exceptions import BusinessException
from app.runtime.docker import DockerSandboxRuntime


class DockerSandboxRuntimeTests(unittest.TestCase):
    def test_require_image_rejects_stale_runtime(self) -> None:
        daemon = subprocess.CompletedProcess([], 0, "27.5.1\n", "")
        stale = subprocess.CompletedProcess([], 0, "0\n", "")
        runtime = DockerSandboxRuntime()
        with (
            patch("app.runtime.docker.shutil.which", return_value="docker"),
            patch("app.runtime.docker.subprocess.run", side_effect=[daemon, stale]),
            self.assertRaisesRegex(BusinessException, "版本过旧"),
        ):
            runtime.require_image(
                "trusted",
                label="org.forgeai.runtime",
                version="1",
                purpose="测试",
            )

    def test_compose_uses_argument_vector_and_trusted_working_directory(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "ready", "")
        with patch("app.runtime.docker.subprocess.run", return_value=completed) as run:
            result = DockerSandboxRuntime.compose(
                "docker",
                workspace=Path("workspace"),
                env_file=Path("secrets.env"),
                project="forgeai-owned",
                arguments=("up", "--build", "-d", "--wait"),
                timeout=900,
            )

        self.assertIs(result, completed)
        self.assertEqual(
            run.call_args.args[0],
            [
                "docker",
                "compose",
                "--env-file",
                "secrets.env",
                "-p",
                "forgeai-owned",
                "up",
                "--build",
                "-d",
                "--wait",
            ],
        )
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["cwd"], Path("workspace"))
        self.assertIn("creationflags", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
