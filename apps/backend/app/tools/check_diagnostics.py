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


@dataclass(frozen=True, slots=True)
class CheckDiagnostic:
    kind: Literal["typescript", "python", "test", "browser", "runtime"]
    message: str
    raw: str
    path: str | None = None
    line: int | None = None
    column: int | None = None
    code: str | None = None

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


def extract_check_diagnostics(output: str) -> tuple[CheckDiagnostic, ...]:
    diagnostics: list[CheckDiagnostic] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        browser_match = _BROWSER_ERROR.match(line)
        match = _TYPESCRIPT_ERROR.match(line)
        if browser_match:
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
            diagnostics.append(
                CheckDiagnostic(
                    kind="typescript",
                    path=match.group("path"),
                    line=int(match.group("line")),
                    column=int(match.group("column")),
                    code=match.group("code").upper(),
                    message=match.group("message"),
                    raw=line,
                )
            )
        elif "SyntaxError:" in line:
            diagnostics.append(CheckDiagnostic(kind="python", message=line, raw=line))
        elif "FAILED:" in line:
            diagnostics.append(CheckDiagnostic(kind="test", message=line, raw=line))
        elif "CalledProcessError:" in line or "RuntimeError:" in line:
            diagnostics.append(CheckDiagnostic(kind="runtime", message=line, raw=line))
    return tuple(diagnostics)


def check_output_excerpt(output: str, *, summary: str = "") -> str:
    """Prefer actionable compiler/test diagnostics over traceback noise."""

    text = output.strip()
    if not text:
        return summary or "检查失败"
    diagnostics = extract_check_diagnostics(text)
    if diagnostics:
        primary = [item.raw for item in diagnostics if item.kind != "runtime"][-2:]
        terminal = [item.raw for item in diagnostics if item.kind == "runtime"][-1:]
        actionable = "\n".join([*primary, *terminal])
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
