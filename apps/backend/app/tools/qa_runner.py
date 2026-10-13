"""Execute an exact QA snapshot, off-network, with no host mounts or model code."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.core.exceptions import BusinessException
from app.core.settings import settings
from app.runtime.docker import get_docker_sandbox_runtime
from app.schemas.agent_action import ToolExecutionResult
from app.schemas.qa_suite import QASuite
from app.services.qa_assets import validate_manifest
from app.tools import checks


def run_acceptance_tests(
    root: Path,
    *,
    code_item_id: str,
    source_hash: str,
    suite: QASuite,
    tool_call_id: str,
) -> ToolExecutionResult:
    result = ToolExecutionResult(
        tool_call_id=tool_call_id,
        name="run_acceptance_tests",
        ok=False,
        summary="",
    )
    identity = {
        "execution_id": f"qa_{uuid4().hex}",
        "code_item_id": code_item_id,
        "source_hash": source_hash,
        "suite_hash": suite.digest(),
    }
    binary = shutil.which("docker")
    if binary is None:
        return result.model_copy(
            update={
                "error_code": "CHECK_ENVIRONMENT_UNAVAILABLE",
                "summary": "独立 QA 需要 Docker 隔离环境",
            }
        )
    name = f"forgeai-qa-{uuid4().hex}"
    try:
        source, digest = checks.source_snapshot(root)
        if digest != source_hash:
            raise BusinessException("SOURCE_MISMATCH")
        payload = json.dumps(
            {**identity, "suite": suite.model_dump(mode="json"), "source": json.loads(source)},
            ensure_ascii=False,
        ).encode("utf-8")
        command = checks.docker_command(binary, settings.engineering_check_image, name, "qa")
        code, output, truncated = checks._execute(
            command,
            payload,
            max(10, min(settings.engineering_check_timeout_seconds, 300)),
        )
        result.data = {**identity, "check_id": "acceptance", "output": output, "exit_code": code}
        result.truncated = truncated
        if code != 0:
            raise BusinessException("隔离执行器无法完成运行：" + output[-500:])
        with TemporaryDirectory(prefix="forgeai-qa-") as directory:
            copied = get_docker_sandbox_runtime().copy_from(
                binary, f"{name}:/tmp/evidence/acceptance.json", Path(directory)
            )
            path = Path(directory) / "acceptance.json"
            if copied.returncode or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                raise BusinessException("独立测试清单缺失或超出预算")
            manifest = json.loads(path.read_text(encoding="utf-8"))
        validate_manifest(manifest, identity, suite)
        if checks.source_snapshot(root)[1] != source_hash:
            raise BusinessException("SOURCE_CHANGED")
        # Never overwrite a prior execution, including failed or blocked attempts.
        destination = root / "forgeai" / "qa" / source_hash / identity["execution_id"]
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "suite.json").write_text(
            suite.model_dump_json(indent=2),
            encoding="utf-8",
        )
        (destination / "execution.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result.data.update(manifest=manifest)
        result.ok = all(row["status"] == "passed" for row in manifest["results"])
        result.error_code = None if result.ok else "ACCEPTANCE_NOT_PASSED"
        result.summary = "独立用例全部通过" if result.ok else "独立用例包含失败、阻塞或未执行项"
    except subprocess.TimeoutExpired:
        result.error_code, result.summary = "CHECK_TIMEOUT", "独立 QA 超时，已清理实例"
    except (BusinessException, OSError, ValueError, UnicodeError) as exc:
        reason = str(exc)
        result.error_code = (
            reason
            if reason in {"SOURCE_CHANGED", "SOURCE_MISMATCH"}
            else "CHECK_ENVIRONMENT_UNAVAILABLE"
        )
        result.summary = reason[:500]
    finally:
        get_docker_sandbox_runtime().remove_container(binary, name)
    return result
