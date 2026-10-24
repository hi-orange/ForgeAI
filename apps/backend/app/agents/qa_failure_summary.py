"""Small, allowlisted QA failure facts for model context.

The caller supplies a platform-validated, source-bound runner manifest. This
projector additionally binds it to the exact suite before looking up a failed
step. It never copies runner evidence, response data, URLs, or check output.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from app.schemas.agent_action import ToolExecutionResult
from app.schemas.qa_suite import APIStep, BrowserStep, QASuite
from app.tools.check_diagnostics import extract_check_diagnostics

MAX_SUMMARY_BYTES = 3 * 1024
MAX_CASES = 16
MAX_DIAGNOSTICS_PER_CHECK = 3

_SAFE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SAFE_DIAGNOSTIC_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,47}\Z")
_SAFE_VISUAL_KEY = re.compile(r"visual(?:_[0-9a-f]{12})?\Z")
_SAFE_JSON_PATH = re.compile(
    r"\$?(?:[A-Za-z_][A-Za-z0-9_]{0,31}|\.[A-Za-z_][A-Za-z0-9_]{0,31}|\[\d{1,4}\]){1,12}\Z"
)
_SELECTOR_ATOM = (
    r"(?:[A-Za-z][A-Za-z0-9_-]*(?:[#.][A-Za-z][A-Za-z0-9_-]*)*"
    r"|[#.][A-Za-z][A-Za-z0-9_-]*"
    r"|\[(?:data-testid|type|name)=(?:['\"]?)[A-Za-z][A-Za-z0-9_-]{0,47}(?:['\"]?)\])"
    r"(?:\[(?:data-testid|type|name)=(?:['\"]?)[A-Za-z][A-Za-z0-9_-]{0,47}(?:['\"]?)\])?"
)
_SAFE_SELECTOR = re.compile(rf"{_SELECTOR_ATOM}(?:\s+{_SELECTOR_ATOM}){{0,2}}\Z")
_SENSITIVE = re.compile(
    r"password|passcode|secret|token|bearer|authorization|api[_-]?key|session|"
    r"private|e-?mail|phone|ssn|@|\d{7,}",
    re.IGNORECASE,
)
_HTTP_MISMATCH = re.compile(
    r"^(?P<method>GET|POST|PUT|PATCH|DELETE) .{1,2000}: "
    r"expected HTTP (?P<expected>[1-5]\d\d), actual (?P<actual>[1-5]\d\d)$"
)
_CHECK_KINDS = frozenset({"typescript", "python", "test", "browser", "runtime", "api"})
_ASSERTION_OPERATORS = frozenset({"equals", "contains", "not_contains", "length", "exists"})
_CASE_STATUSES = frozenset({"failed", "blocked", "not_run"})
_FAILURE_KINDS = frozenset({"product", "script", "environment", "conflict"})


def _bytes(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _safe_identifier(value: object) -> str | None:
    if isinstance(value, str) and _SAFE_ID.fullmatch(value) and not _SENSITIVE.search(value):
        return value
    return None


def _safe_selector(value: object) -> str | None:
    if (
        isinstance(value, str)
        and len(value) <= 120
        and _SAFE_SELECTOR.fullmatch(value)
        and not _SENSITIVE.search(value)
    ):
        return value
    return None


def _safe_json_path(value: object) -> str | None:
    if (
        isinstance(value, str)
        and len(value) <= 100
        and _SAFE_JSON_PATH.fullmatch(value)
        and not _SENSITIVE.search(value)
    ):
        return value
    return None


def _safe_relative_path(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 128 or _SENSITIVE.search(value):
        return None
    path = value.replace("\\", "/")
    parts = path.split("/")
    if (
        not parts
        or parts[0] not in {"frontend", "backend", "src", "app", "tests"}
        or any(part in {"", ".", ".."} for part in parts)
        or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)
    ):
        return None
    return path


def _failure_category(evidence: object, *, kind: str | None) -> str:
    """Classify without allowing any literal runner output into the model."""

    if not isinstance(evidence, str):
        return kind or "unknown"
    lower = evidence.lower()
    if "strict mode violation" in lower:
        return "non_unique_locator"
    if "timeout" in lower or "timed out" in lower:
        return "timeout"
    if "net::" in lower or "connection refused" in lower:
        return "connection_failure"
    if "assert" in lower or "expected" in lower:
        return "assertion_mismatch"
    return kind or "unknown"


def _failed_step(row: Mapping[str, Any], suite: QASuite) -> APIStep | BrowserStep | None:
    test_id = row.get("test_id")
    script = suite.scripts.get(test_id) if isinstance(test_id, str) else None
    observations = row.get("observations")
    if script is None or not isinstance(observations, list):
        return None
    index = len(observations)
    if index >= len(script.steps) or any(
        not isinstance(observed, dict) or observed.get("kind") != script.steps[i].kind
        for i, observed in enumerate(observations)
    ):
        return None
    return script.steps[index]


def _case_fact(row: Mapping[str, Any], suite: QASuite) -> dict[str, Any]:
    status = row["status"]
    kind = row.get("failure_kind")
    result: dict[str, Any] = {"status": status}
    test_id = _safe_identifier(row.get("test_id"))
    if test_id is not None:
        result["test_id"] = test_id
    if kind in _FAILURE_KINDS:
        result["failure_kind"] = kind
    step = _failed_step(row, suite)
    if step is None:
        return result
    observations = row["observations"]
    result["failed_step_index"] = len(observations)
    if isinstance(step, BrowserStep):
        result["action"] = step.action
        result["category"] = _failure_category(row.get("evidence"), kind=kind)
        selector = _safe_selector(step.selector)
        if selector is not None:
            result["selector"] = selector
        return result

    result["method"] = step.method
    result["expected_http_status"] = step.expect_status
    evidence = row.get("evidence")
    if not isinstance(evidence, str):
        return result
    mismatch = _HTTP_MISMATCH.fullmatch(evidence)
    if (
        mismatch is not None
        and mismatch.group("method") == step.method
        and int(mismatch.group("expected")) == step.expect_status
    ):
        result["actual_http_status"] = int(mismatch.group("actual"))
        result["category"] = "http_status_mismatch"
        return result
    for assertion in step.assertions:
        path = _safe_json_path(assertion.path)
        if path is None or assertion.operator not in _ASSERTION_OPERATORS:
            continue
        if evidence.startswith(f"{assertion.path} {assertion.operator}:"):
            result["json_assertion"] = {"path": path, "operator": assertion.operator}
            result["category"] = "json_assertion_mismatch"
            break
    return result


def _check_fact(result: ToolExecutionResult) -> dict[str, Any]:
    fact: dict[str, Any] = {"ok": result.ok}
    if result.ok:
        return fact
    code = result.error_code
    if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code):
        fact["error_code"] = code
    output = result.data.get("output")
    if not isinstance(output, str):
        return fact
    diagnostics = []
    for item in extract_check_diagnostics(output[-32_768:]):
        if item.kind not in _CHECK_KINDS:
            continue
        diagnostic: dict[str, Any] = {"kind": item.kind}
        code = _safe_identifier(item.code)
        if code is not None and _SAFE_DIAGNOSTIC_CODE.fullmatch(code):
            diagnostic["code"] = code
        path = _safe_relative_path(item.path)
        if path is not None:
            diagnostic["path"] = path
        if type(item.line) is int and 1 <= item.line <= 1_000_000 and path is not None:
            diagnostic["line"] = item.line
        diagnostics.append(diagnostic)
        if len(diagnostics) == MAX_DIAGNOSTICS_PER_CHECK:
            break
    if diagnostics:
        fact["diagnostics"] = diagnostics
    return fact


def summarize_qa_failures(
    manifest: Mapping[str, Any] | None,
    suite: QASuite | None,
    checks: Mapping[str, ToolExecutionResult] | None = None,
) -> dict[str, Any]:
    """Return at most 16 current nonpasses within a 3 KiB JSON payload.

    All facts are allowlisted. In particular, neither `evidence` nor check
    `output` appears verbatim, even when it carries a useful error message.
    """

    summary: dict[str, Any] = {"nonpassed_total": 0, "cases": [], "checks": {}}
    visual_key = (
        next(
            (
                check_id
                for check_id, result in reversed(list(checks.items()))
                if _SAFE_VISUAL_KEY.fullmatch(check_id) and result.name == "capture_screenshots"
            ),
            None,
        )
        if checks is not None
        else None
    )
    for check_id in ("all", visual_key):
        result = checks.get(check_id) if checks is not None and check_id is not None else None
        if result is None:
            continue
        fact = _check_fact(result)
        candidate = {**summary, "checks": {**summary["checks"], check_id: fact}}
        if _bytes(candidate) <= MAX_SUMMARY_BYTES:
            summary = candidate

    if (
        manifest is None
        or suite is None
        or manifest.get("suite_hash") != suite.digest()
        or not isinstance(manifest.get("results"), list)
    ):
        return summary
    nonpassed = [
        row
        for row in manifest["results"]
        if isinstance(row, dict) and row.get("status") in _CASE_STATUSES
    ]
    summary["nonpassed_total"] = len(nonpassed)
    summary["omitted_case_count"] = len(nonpassed)
    for row in nonpassed[:MAX_CASES]:
        case = _case_fact(row, suite)
        candidate = {**summary, "cases": [*summary["cases"], case]}
        candidate["omitted_case_count"] -= 1
        if _bytes(candidate) > MAX_SUMMARY_BYTES:
            break
        summary = candidate
    return summary
