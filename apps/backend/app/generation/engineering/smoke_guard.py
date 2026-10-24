"""Keep engineering smoke coverage from becoming a passing substitute."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from app.schemas.agent_action import ToolExecutionResult
from app.tools.check_diagnostics import extract_check_diagnostics
from app.tools.paths import sha256_bytes
from sandbox.smoke_manifest import SMOKE_CONTRACT, parse_smoke_manifest

_VARIABLE = re.compile(r"\$\{[A-Za-z][A-Za-z0-9_]*\}")
_OBSERVABLE_ACTIONS = frozenset(
    {
        "expect_text",
        "expect_any_text",
        "expect_path",
        "expect_count",
        "expect_no_horizontal_scroll",
        "screenshot",
    }
)
_BASELINE_KEY = "smoke_probe_baseline_v1"


def _path_shape(value: str) -> str:
    """Allow a fixed fixture id to become a captured id, without changing routes."""
    path = urlsplit(value).path
    return "/".join(
        "{id}" if segment.isdecimal() else _VARIABLE.sub("{id}", segment)
        for segment in path.split("/")
    )


def _template_shape(value: Any) -> Any:
    """Ignore capture variable names while preserving literal request input."""
    if isinstance(value, str):
        return _VARIABLE.sub("${value}", value)
    if isinstance(value, list):
        return [_template_shape(item) for item in value]
    if isinstance(value, dict):
        return {key: _template_shape(item) for key, item in value.items()}
    return value


def _json_type_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_type_shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_type_shape(item) for item in value]
    return type(value).__name__


def _api_contract(check: dict[str, Any]) -> dict[str, Any]:
    path = str(check.get("path") or "")
    headers = check.get("headers") or {}
    return {
        "method": str(check.get("method") or "GET").upper(),
        "path": _path_shape(path),
        "query": _query_params(path),
        "expect_status": check.get("expect_status", 200),
        "expect_json_paths": list(check.get("expect_json_paths") or []),
        "capture_paths": list((check.get("capture") or {}).values()),
        "authorization": any(key.casefold() == "authorization" for key in headers),
        "json": check.get("json"),
    }


def _browser_expectation(expected: Any) -> str:
    if isinstance(expected, str):
        return json.dumps(
            {"prefix": _path_shape(expected), "query_keys": _query_keys(expected)},
            ensure_ascii=False,
            sort_keys=True,
        )
    return json.dumps(
        {
            "method": expected.get("method"),
            "path": _path_shape(str(expected.get("path") or "")),
            "query_keys": _query_keys(str(expected.get("path") or "")),
            "status": expected.get("status"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _observable_action(action: dict[str, Any]) -> str:
    action_type = action.get("type")
    if action_type == "expect_count":
        # Fixture cardinality can change, but a positive observation cannot
        # become an empty-state assertion or vice versa.
        return f"expect_count:{'zero' if action.get('count') == 0 else 'positive'}"
    return str(action_type)


def _browser_contract(check: dict[str, Any]) -> dict[str, Any]:
    browser = SMOKE_CONTRACT["browser"]
    return {
        "route": _path_shape(str(check.get("route") or "")),
        "kind": check.get("kind", browser["default_kind"]),
        "viewport": check.get("viewport", browser["default_viewport"]),
        "expect_api": [_browser_expectation(item) for item in check.get("expect_api") or []],
        "assertions": [
            _observable_action(item)
            for item in check.get("actions") or []
            if item.get("type") in _OBSERVABLE_ACTIONS
        ]
        + ["expect_text"] * len(check.get("expect_text") or []),
    }


def ensure_smoke_probe_baseline(checkpoint: dict[str, Any], manifest_path: Path) -> None:
    """Freeze inherited probes once; rejected staged writes cannot change this ledger."""
    if _BASELINE_KEY in checkpoint:
        return
    content = manifest_path.read_bytes()
    root = manifest_path.parent.resolve()
    metadata = json.loads((root / "template.json").read_text(encoding="utf-8"))
    manifest = parse_smoke_manifest(content.decode("utf-8"), root=root, template_metadata=metadata)
    checkpoint[_BASELINE_KEY] = {
        "initial_manifest_hash": sha256_bytes(content),
        "api_checks": {check["id"]: _api_contract(check) for check in manifest["api_checks"]},
        "browser_checks": {
            check["id"]: _browser_contract(check) for check in manifest["browser_checks"]
        },
    }


def smoke_probe_inventory_violation(checkpoint: dict[str, Any], manifest: object) -> str | None:
    """Report removed probes before detailed schema errors obscure the coverage loss."""
    baseline = checkpoint.get(_BASELINE_KEY)
    if not isinstance(baseline, dict):
        return None
    if not isinstance(manifest, dict):
        return None  # The schema validator reports malformed input.
    ids = {}
    for field in ("api_checks", "browser_checks"):
        checks = manifest.get(field)
        if not isinstance(checks, list):
            return None  # The schema validator gives the precise error.
        ids[field] = {
            check["id"]
            for check in checks
            if isinstance(check, dict) and isinstance(check.get("id"), str)
        }
    dropped = {
        field: sorted(set(baseline.get(field) or {}) - ids[field])
        for field in ("api_checks", "browser_checks")
    }
    if any(dropped.values()):

        def preview(ids: list[str]) -> str:
            return ", ".join(ids[:5]) + (f" 等 {len(ids)} 项" if len(ids) > 5 else "")

        return (
            f"smoke 清单删除了 {len(dropped['api_checks'])} 个 API 探针"
            f"（{preview(dropped['api_checks']) or '无'}）和 "
            f"{len(dropped['browser_checks'])} 个浏览器探针"
            f"（{preview(dropped['browser_checks']) or '无'}）。"
            "保留旧探针 ID，仅局部调整与获批新需求冲突的数据准备或断言；"
            "可用 edit_file_by_replace，避免整份重写。"
        )
    return None


def _baseline_violation(checkpoint: dict[str, Any], manifest: object) -> str | None:
    baseline = checkpoint.get(_BASELINE_KEY)
    if not isinstance(baseline, dict):
        return None
    if not isinstance(manifest, dict):
        return "forgeai.smoke.json 已缺失；恢复原清单后重试 all 检查。"
    inventory_issue = smoke_probe_inventory_violation(checkpoint, manifest)
    if inventory_issue is not None:
        return inventory_issue
    current = {}
    for field in ("api_checks", "browser_checks"):
        checks = manifest.get(field)
        if not isinstance(checks, list):
            return None  # The schema validator gives the precise error.
        current[field] = {check.get("id"): check for check in checks if isinstance(check, dict)}
    for probe_id, previous in (baseline.get("api_checks") or {}).items():
        check = _api_contract(current["api_checks"][probe_id])
        label = f"API 探针 {probe_id}"
        for field, description in (
            ("method", "请求方法"),
            ("path", "请求路径"),
            ("expect_status", "预期状态码"),
            ("authorization", "身份凭据边界"),
        ):
            if check[field] != previous[field]:
                return f"{label} 的{description}被替换；保留原请求合同，另加新探针验证新行为。"
        old_query = previous["query"]
        new_query = check["query"]
        if [pair[0] for pair in old_query] != [pair[0] for pair in new_query]:
            return f"{label} 的查询参数被删减或替换；保留原筛选维度。"
        if previous["expect_status"] >= 400:
            if _template_shape(old_query) != _template_shape(new_query):
                return f"{label} 的负向请求条件被修改；保留原错误场景。"
        elif any(
            bool(old[1]) and not bool(new[1]) for old, new in zip(old_query, new_query, strict=True)
        ):
            return f"{label} 的查询值被清空；保留真实筛选输入。"
        if not set(previous["expect_json_paths"]).issubset(check["expect_json_paths"]):
            return f"{label} 的响应字段断言被删减；恢复原断言。"
        if not Counter(previous["capture_paths"]) <= Counter(check["capture_paths"]):
            return f"{label} 的响应变量捕获被删减；可改变量名但须保留捕获字段。"
        old_body, new_body = previous["json"], check["json"]
        if previous["expect_status"] >= 400:
            if _template_shape(old_body) != _template_shape(new_body):
                return f"{label} 的负向请求数据被修改；保留原错误输入。"
        elif _json_type_shape(old_body) != _json_type_shape(new_body):
            return f"{label} 的请求数据结构被删减或替换；保留原输入字段。"
    for probe_id, previous in (baseline.get("browser_checks") or {}).items():
        check = _browser_contract(current["browser_checks"][probe_id])
        label = f"浏览器探针 {probe_id}"
        for field, description in (
            ("route", "入口路由"),
            ("kind", "探针类型"),
            ("viewport", "视口"),
        ):
            if check[field] != previous[field]:
                return f"{label} 的{description}被修改；保留原用户场景。"
        if not Counter(previous["expect_api"]) <= Counter(check["expect_api"]):
            return f"{label} 的前后端请求断言被删减；恢复原 expect_api。"
        if not Counter(previous["assertions"]) <= Counter(check["assertions"]):
            return f"{label} 的可观察断言被删减；保留旧断言并可增加新断言。"
    return None


def _extend_verified_baseline(checkpoint: dict[str, Any], manifest: object) -> None:
    """A passing full run makes newly added probes durable obligations."""
    baseline = checkpoint.get(_BASELINE_KEY)
    if not isinstance(baseline, dict) or not isinstance(manifest, dict):
        return
    for field, contract in (
        ("api_checks", _api_contract),
        ("browser_checks", _browser_contract),
    ):
        ledger = baseline.get(field)
        checks = manifest.get(field)
        if not isinstance(ledger, dict) or not isinstance(checks, list):
            continue
        for check in checks:
            if isinstance(check, dict) and isinstance(check.get("id"), str):
                ledger.setdefault(check["id"], contract(check))


def _request_path(value: str) -> str:
    return urlsplit(value).path


def _query_keys(value: str) -> list[str]:
    return sorted(key for key, _ in parse_qsl(urlsplit(value).query, keep_blank_values=True))


def _query_params(value: str) -> list[list[str]]:
    """Compare decoded query pairs as a multiset, independent of their order."""

    return [list(pair) for pair in sorted(parse_qsl(urlsplit(value).query, keep_blank_values=True))]


def _query_value_matches(template: str, observed: str) -> bool:
    matches = list(_VARIABLE.finditer(template))
    if not matches:
        return template == observed
    fragments: list[str] = []
    cursor = 0
    for match in matches:
        fragments.extend((re.escape(template[cursor : match.start()]), ".*"))
        cursor = match.end()
    fragments.append(re.escape(template[cursor:]))
    return re.fullmatch("".join(fragments), observed, flags=re.DOTALL) is not None


def _rendered_query_matches(manifest_path: str, observed: list[list[str]]) -> bool:
    """Match literal values exactly while allowing captured runtime variables."""

    declared = _query_params(manifest_path)
    if len(declared) != len(observed):
        return False
    remaining = [list(pair) for pair in observed]
    # Consume literals first when a key occurs more than once. A placeholder
    # must not hide a changed literal value in the same query.
    for key, value in sorted(declared, key=lambda pair: bool(_VARIABLE.search(pair[1]))):
        match = next(
            (
                index
                for index, (actual_key, actual_value) in enumerate(remaining)
                if actual_key == key and _query_value_matches(value, actual_value)
            ),
            None,
        )
        if match is None:
            return False
        remaining.pop(match)
    return not remaining


def _same_request(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return all(
        first.get(field) == second.get(field)
        for field in ("method", "path", "query_keys", "expect_status")
    ) and (
        "query_params" not in first
        or "query_params" not in second
        or first["query_params"] == second["query_params"]
    )


def _failed_api_probes(result: dict[str, Any]) -> list[dict[str, Any]]:
    if result.get("ok") is not False:
        return []
    output = result.get("data", {}).get("output")
    if not isinstance(output, str):
        return []
    probes = []
    for diagnostic in extract_check_diagnostics(output):
        if (
            diagnostic.kind != "api"
            or not diagnostic.code
            or not diagnostic.method
            or not diagnostic.route
            or diagnostic.actual != "404"
            or not diagnostic.expected
            or not 200 <= int(diagnostic.expected) < 300
        ):
            continue
        probes.append(
            {
                "id": diagnostic.code,
                "method": diagnostic.method,
                "path": _request_path(diagnostic.route),
                "query_keys": _query_keys(diagnostic.route),
                "query_params": _query_params(diagnostic.route),
                "expect_status": int(diagnostic.expected),
            }
        )
    return probes


def protected_api_probes(checkpoint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Recover older failed probes as well as probes frozen by the current runner.

    Old checkpoints have no dedicated smoke ledger. Their append-only diagnostic
    history still identifies an executed request, even if the mutable manifest
    was already replaced before this guard was installed.
    """

    completed = {
        item.get("id")
        for item in checkpoint.get("work_items") or []
        if isinstance(item, dict) and item.get("status") == "completed"
    }
    completed_turn = max(
        (
            int(row["check"]["model_turn"])
            for row in checkpoint.get("plan_history") or []
            if isinstance(row, dict)
            and isinstance(row.get("plan"), dict)
            and row["plan"].get("work_item_id") in completed
            and isinstance(row.get("check"), dict)
            and isinstance(row["check"].get("model_turn"), int)
        ),
        default=0,
    )
    probes: dict[str, dict[str, Any]] = {}
    history = checkpoint.get("diagnostic_history")
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict) or not str(entry.get("scope", "")).startswith("check:"):
                continue
            if int(entry.get("model_turn") or 0) < completed_turn:
                continue
            result = entry.get("result")
            if isinstance(result, dict):
                for probe in _failed_api_probes(result):
                    probes.setdefault(probe["id"], probe)
    repair = checkpoint.get("repair_context")
    if isinstance(repair, dict) and isinstance(repair.get("failed_check"), dict):
        for probe in _failed_api_probes(repair["failed_check"]):
            probes.setdefault(probe["id"], probe)
    recorded = checkpoint.get("smoke_failure_probes")
    if isinstance(recorded, dict):
        for probe_id, probe in recorded.items():
            if isinstance(probe_id, str) and isinstance(probe, dict):
                historical = probes.get(probe_id)
                if historical is None:
                    probes[probe_id] = probe
                elif _same_request(historical, probe):
                    # A full current-run snapshot enriches old status evidence;
                    # it must never redefine the originally failed request.
                    probes[probe_id] = {**historical, **probe}
    return probes


def hydrate_legacy_failed_api_probes(checkpoint: dict[str, Any]) -> None:
    """Carry an old checkpoint's active failed request into later work items."""

    ledger = checkpoint.setdefault("smoke_failure_probes", {})
    for probe_id, probe in protected_api_probes(checkpoint).items():
        ledger.setdefault(probe_id, probe)


def record_failed_api_probes(
    checkpoint: dict[str, Any], result: ToolExecutionResult, manifest_path: Path
) -> None:
    """Preserve the full active probe contract when a real API check fails."""

    failures = _failed_api_probes(result.model_dump(mode="json"))
    if not failures:
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # The check runner reports manifest parsing failures separately.
    checks = manifest.get("api_checks") if isinstance(manifest, dict) else None
    if not isinstance(checks, list):
        return
    by_id = {row.get("id"): row for row in checks if isinstance(row, dict)}
    ledger = checkpoint.setdefault("smoke_failure_probes", {})
    historical = protected_api_probes(checkpoint)
    for failure in failures:
        prior = historical.get(failure["id"])
        if prior is not None and not _same_request(prior, failure):
            continue
        check = by_id.get(failure["id"])
        if not isinstance(check, dict):
            continue
        if (
            str(check.get("method") or "GET").upper() != failure["method"]
            or _request_path(str(check.get("path") or "")) != failure["path"]
            or not _rendered_query_matches(str(check.get("path") or ""), failure["query_params"])
            or check.get("expect_status", 200) != failure["expect_status"]
        ):
            continue
        if failure["id"] in ledger and "expect_json_paths" in ledger[failure["id"]]:
            continue
        # Use the request observed by the check runner as identity; a later
        # manifest rewrite cannot redefine it to a different endpoint.
        ledger[failure["id"]] = {
            **failure,
            "manifest_query_params": _query_params(str(check.get("path") or "")),
            "expect_json_paths": list(check.get("expect_json_paths") or []),
            "capture": dict(check.get("capture") or {}),
            "requires_authorization": any(
                key.casefold() == "authorization" for key in (check.get("headers") or {})
            ),
            "work_item_id": checkpoint.get("current_work_item_id"),
        }


def smoke_probe_violation(checkpoint: dict[str, Any], manifest: object) -> str | None:
    """Return an actionable violation; additive and credential fixes remain legal."""

    baseline_issue = _baseline_violation(checkpoint, manifest)
    if baseline_issue is not None:
        return baseline_issue
    protected = protected_api_probes(checkpoint)
    if not protected:
        return None
    checks = manifest.get("api_checks") if isinstance(manifest, dict) else None
    if not isinstance(checks, list):
        return None  # The schema validator reports this more precisely.
    by_id = {row.get("id"): row for row in checks if isinstance(row, dict)}
    for probe_id, baseline in protected.items():
        check = by_id.get(probe_id)
        expected_method = str(baseline["method"]).upper()
        expected_path = str(baseline["path"])
        required_status = int(baseline["expect_status"])
        identity = f"{expected_method} {expected_path} → HTTP {required_status}"
        problem = None
        if check is None:
            problem = "已被删除"
        elif str(check.get("method") or "GET").upper() != expected_method:
            problem = "请求方法被替换"
        elif _request_path(str(check.get("path") or "")) != expected_path:
            problem = f"请求路径被替换为 {check.get('path')}"
        elif _query_keys(str(check.get("path") or "")) != baseline.get("query_keys", []):
            problem = "查询参数被删减或替换"
        elif ("manifest_query_params" in baseline or "query_params" in baseline) and _query_params(
            str(check.get("path") or "")
        ) != baseline.get("manifest_query_params", baseline.get("query_params")):
            problem = "查询参数被删减或替换"
        elif check.get("expect_status", 200) != required_status:
            problem = "预期状态码被修改"
        elif not set(baseline.get("expect_json_paths") or []).issubset(
            check.get("expect_json_paths") or []
        ):
            problem = "响应字段断言被删减"
        elif any(
            check.get("capture", {}).get(name) != path
            for name, path in (baseline.get("capture") or {}).items()
        ):
            problem = "响应变量捕获被删减或替换"
        elif baseline.get("requires_authorization") is False and any(
            key.casefold() == "authorization" for key in (check.get("headers") or {})
        ):
            problem = "原无身份凭据的请求被改成需登录请求"
        if problem is None:
            continue
        acceptance = next(
            (
                item.get("acceptance") or []
                for item in checkpoint.get("work_items") or []
                if isinstance(item, dict)
                and item.get("id") == checkpoint.get("current_work_item_id")
            ),
            [],
        )
        criterion = str(acceptance[0])[:180] if acceptance else "当前已批准验收"
        return (
            f"smoke 探针 {probe_id} 的已失败请求 {identity} {problem}。"
            "恢复原请求和断言；若它对应当前验收，现在就实现最小真实接口及调用方，"
            "后续工作项再扩展功能，不能用另一条私有接口、删探针或降断言制造通过。"
            f"当前工作项验收：{criterion}"
        )
    return None


def guard_passing_smoke_check(
    checkpoint: dict[str, Any], result: ToolExecutionResult, manifest_path: Path
) -> ToolExecutionResult:
    """A passing runner result cannot green-light a weakened smoke manifest."""

    if not result.ok or result.data.get("check_id") != "all":
        return result
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None  # A missing manifest must not pass a recorded baseline.
    violation = smoke_probe_violation(checkpoint, manifest)
    if violation is None:
        _extend_verified_baseline(checkpoint, manifest)
        return result
    return result.model_copy(
        update={
            "ok": False,
            "error_code": "SMOKE_PROBE_WEAKENED",
            "summary": violation,
            "data": {**result.data, "phase": "smoke_probe_guard", "output": violation},
        }
    )
