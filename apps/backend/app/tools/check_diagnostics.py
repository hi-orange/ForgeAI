"""Pure parsing of check-runner failures into stable, structured diagnostics."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, Literal

from app.schemas.agent_action import ToolExecutionResult

MAX_CHECK_EXCERPT_CHARS = 500
MAX_CHECK_FINGERPRINT_CHARS = 8_000

_TYPESCRIPT_ERROR = re.compile(
    r"^(?P<path>.+?)\((?P<line>\d+),(?P<column>\d+)\):\s*"
    r"error\s+(?P<code>TS\d+):\s*(?P<message>.+)$",
    flags=re.IGNORECASE,
)
_BROWSER_ERROR = re.compile(
    r"^BROWSER_CHECK_FAILED\s+(?P<check>\S+)\s+(?P<route>\S+):\s*(?P<message>.+)$"
)
_API_STATUS_ERROR = re.compile(
    r"^(?:RuntimeError: )?API 联调失败 (?P<check>[^：]+)：期望 HTTP "
    r"(?P<expected>\d+)，实际 (?P<actual>\d+)；(?P<detail>.*)$"
)
_HTTP_ACCESS_LOG = re.compile(
    r'"(?P<method>[A-Z]+) (?P<route>/\S+) HTTP/[^\s"]+"\s+(?P<status>\d{3})\b'
)
_TRACEBACK_FILE = re.compile(r'^\s*File\s+"(?P<path>[^"]+)",\s+line\s+(?P<line>\d+)')
_PYTHON_EXCEPTION = re.compile(
    r"^(?:(?:[\w]+\.)+)?(?P<code>"
    r"ModuleNotFoundError|ImportError|SyntaxError|IndentationError|"
    r"AttributeError|NameError|TypeError|ValueError|KeyError|"
    r"RuntimeError|CalledProcessError|PydanticSchemaGenerationError"
    r"):\s*(?P<message>.+)$"
)
# Prefer import/root failures over wrapper process errors when ranking excerpts.
_PYTHON_ROOT_CODES = frozenset(
    {
        "ModuleNotFoundError",
        "ImportError",
        "SyntaxError",
        "IndentationError",
        "AttributeError",
        "NameError",
        "PydanticSchemaGenerationError",
    }
)


@dataclass(frozen=True, slots=True)
class CheckDiagnostic:
    kind: Literal["typescript", "python", "test", "browser", "runtime", "api"]
    message: str
    raw: str
    path: str | None = None
    line: int | None = None
    column: int | None = None
    code: str | None = None
    expected: str | None = None
    actual: str | None = None
    symbol: str | None = None
    related_module: str | None = None
    method: str | None = None
    route: str | None = None
    suggested_action: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CheckFailureAnalysis:
    fingerprint: str
    excerpt: str
    diagnostics: tuple[CheckDiagnostic, ...]


def normalize_check_output(output: str) -> str:
    """Stabilize volatile tokens so identical failures fingerprint the same."""

    text = output.strip()
    text = re.sub(r"line\s+\d+", "line #", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(process|port)\s+\d+\b", r"\1 #", text, flags=re.IGNORECASE)
    text = re.sub(r"\bprocess\s+\[\d+\]", "process [#]", text, flags=re.IGNORECASE)
    return re.sub(r"(?<=\b127\.0\.0\.1:)\d+", "#", text)


def _workspace_relative_path(path: str) -> str | None:
    """Map a traceback path onto the generated workspace.

    The check container roots the workspace at ``/tmp/app``. A generic ``/app/``
    marker matches that root first and would report ``app/backend/...`` instead
    of ``backend/...``. Prefer a path that already starts at ``backend/`` or
    ``frontend/``.
    """

    normalized = path.replace("\\", "/")
    candidates: list[str] = []
    for root in ("/tmp/app/", "/workspace/"):
        index = normalized.find(root)
        if index >= 0:
            relative = normalized[index + len(root) :]
            if relative:
                candidates.append(relative)
    for marker in ("/backend/", "/frontend/", "/app/"):
        index = normalized.find(marker)
        if index >= 0:
            relative = normalized[index + 1 :]
            if relative.startswith("workspace/"):
                relative = relative[len("workspace/") :]
            if relative:
                candidates.append(relative)
    for relative in candidates:
        if relative.startswith(("backend/", "frontend/")):
            return relative
    if normalized.startswith(("backend/", "frontend/")):
        return normalized
    return candidates[0] if candidates else None


def extract_check_diagnostics(output: str) -> tuple[CheckDiagnostic, ...]:
    diagnostics: list[CheckDiagnostic] = []
    last_file: str | None = None
    last_line: int | None = None
    last_request: tuple[str, str, str] | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        request_match = _HTTP_ACCESS_LOG.search(line)
        if request_match and request_match.group("route").startswith("/api/v1/"):
            last_request = (
                request_match.group("method"),
                request_match.group("route"),
                request_match.group("status"),
            )
        file_match = _TRACEBACK_FILE.match(raw_line)
        if file_match:
            # Keep the last generated-workspace frame. Library frames after it
            # (pydantic, uvicorn) are not files the repair planner can edit.
            relative = _workspace_relative_path(file_match.group("path"))
            if relative is not None:
                last_file = relative
                last_line = int(file_match.group("line"))
            continue
        browser_match = _BROWSER_ERROR.match(line)
        match = _TYPESCRIPT_ERROR.match(line)
        python_match = _PYTHON_EXCEPTION.match(line)
        api_match = _API_STATUS_ERROR.match(line)
        if api_match:
            actual = api_match.group("actual")
            request = last_request if last_request and last_request[2] == actual else None
            expected = api_match.group("expected")
            missing_successful_api = actual == "404" and 200 <= int(expected) < 300
            diagnostics.append(
                CheckDiagnostic(
                    kind="api",
                    code=api_match.group("check"),
                    message=line,
                    raw=line,
                    expected=expected,
                    actual=actual,
                    method=request[0] if request else None,
                    route=request[1] if request else None,
                    suggested_action=(
                        "按此探针 id 核对 forgeai.smoke.json 中的方法、路径和已批准验收；"
                        "检查实际注册的后端路由（如 /openapi.json）及对应查询。"
                        "若批准功能要求该请求成功，补齐真实接口和响应；"
                        "保留等价强度的探针，只有证实探针地址错误时才修正它。"
                        if missing_successful_api
                        else None
                    ),
                )
            )
        elif browser_match:
            diagnostics.append(
                CheckDiagnostic(
                    kind="browser",
                    code=browser_match.group("check"),
                    message=(
                        f"浏览器检查 {browser_match.group('check')} "
                        f"({browser_match.group('route')})：{browser_match.group('message')}"
                    ),
                    raw=line,
                )
            )
        elif match:
            message = match.group("message")
            export = re.search(
                r"""Module\s+['"](.+?)['"]\s+has no exported member\s+['"]([^'"]+)['"]""",
                message,
            )
            diagnostics.append(
                CheckDiagnostic(
                    kind="typescript",
                    path=match.group("path"),
                    line=int(match.group("line")),
                    column=int(match.group("column")),
                    code=match.group("code").upper(),
                    message=message,
                    raw=line,
                    symbol=export.group(2) if export else None,
                    related_module=export.group(1).strip('"') if export else None,
                )
            )
        elif python_match:
            code = python_match.group("code")
            kind: Literal["python", "runtime", "test"] = (
                "runtime" if code in {"RuntimeError", "CalledProcessError"} else "python"
            )
            if code == "CalledProcessError" and "FAILED:" in output:
                # Keep FAILED: lines as the test identity; process wrapper is noise.
                kind = "runtime"
            diagnostics.append(
                CheckDiagnostic(
                    kind=kind,
                    path=last_file if kind == "python" else None,
                    line=last_line if kind == "python" else None,
                    code=code,
                    message=python_match.group("message"),
                    raw=line,
                )
            )
        elif "FAILED:" in line:
            diagnostics.append(CheckDiagnostic(kind="test", message=line, raw=line))
    return tuple(diagnostics)


def repair_verification(result: ToolExecutionResult) -> dict[str, Any]:
    """Recommend the narrowest useful recheck; never turn it into delivery evidence."""
    if result.error_code == "API_CLIENT_STALE":
        return {"tool": "generate_api_client", "then": "complete_work_item", "delivery_pass": False}
    failed = str(result.data.get("check_id") or "all")
    diagnostics = extract_check_diagnostics(str(result.data.get("output") or ""))
    actionable = [d for d in diagnostics if d.code != "CalledProcessError"]
    check_id = failed if failed in {"quick", "database", "backend", "frontend", "all"} else "all"
    if actionable and all(
        d.kind == "typescript" or d.code in {"SyntaxError", "IndentationError"} for d in actionable
    ):
        check_id = "quick"
    elif actionable and all(d.code in {"ImportError", "ModuleNotFoundError"} for d in actionable):
        check_id = "backend"
    return {
        "tool": "run_check",
        "check_id": check_id,
        "failed_check_id": failed,
        "then": "complete_work_item",
        "delivery_pass": False,
        "targets": [
            {
                "path": f"frontend/{d.path}"
                if d.kind == "typescript" and d.path and d.path.startswith("src/")
                else d.path,
                "line": d.line,
                "symbol": d.symbol,
                "related_module": d.related_module,
            }
            for d in actionable
            if d.path
        ][:20],
    }


def check_output_excerpt(output: str, *, summary: str = "") -> str:
    """Prefer actionable compiler/import diagnostics over traceback noise."""

    text = output.strip()
    if not text:
        return summary or "检查失败"
    diagnostics = extract_check_diagnostics(text)
    if diagnostics:
        root_python = [
            item.raw
            for item in diagnostics
            if item.kind == "python" and item.code in _PYTHON_ROOT_CODES
        ]
        primary = [
            item.raw
            for item in diagnostics
            if item.kind in {"typescript", "browser", "test", "python", "api"}
            and item.code not in _PYTHON_ROOT_CODES
        ][-2:]
        # Surface import/module roots first so repair planners see the real cause.
        actionable = "\n".join([*root_python[-2:], *primary])
        if not actionable.strip():
            terminal = [item.raw for item in diagnostics if item.kind == "runtime"][-1:]
            actionable = "\n".join(terminal)
        return actionable[-MAX_CHECK_EXCERPT_CHARS:]
    if len(text) <= MAX_CHECK_EXCERPT_CHARS:
        return text
    return text[-MAX_CHECK_EXCERPT_CHARS:]


def analyze_check_failure(result: ToolExecutionResult) -> CheckFailureAnalysis:
    """Return stable identity, UI excerpt, and parsed diagnostics for one failure."""

    output = str(result.data.get("output") or "")
    excerpt = check_output_excerpt(
        output,
        summary=str(result.summary or result.error_code or "检查失败"),
    )
    diagnostics = extract_check_diagnostics(output)
    actionable = [item for item in diagnostics if item.kind != "runtime"]
    if actionable:
        # OpenHands-style semantic comparison: ignore raw tracebacks, PIDs,
        # ports and event IDs. The compiler/test identity is the progress signal.
        body = json.dumps(
            [
                {
                    "kind": item.kind,
                    "path": item.path,
                    "line": item.line,
                    "column": item.column,
                    "code": item.code,
                    "message": item.message,
                    "method": item.method,
                    "route": item.route,
                }
                for item in actionable
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        normalized = normalize_check_output(output)
        body = normalized[-MAX_CHECK_FINGERPRINT_CHARS:] if normalized else excerpt
    payload = f"{result.error_code or ''}:{body}".encode()
    return CheckFailureAnalysis(
        fingerprint=sha256(payload).hexdigest(),
        excerpt=excerpt,
        diagnostics=diagnostics,
    )
