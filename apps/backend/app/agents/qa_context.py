"""Bounded model context projected from the authoritative QA checkpoint."""

from __future__ import annotations

import json
import re
from typing import Any

from app.agents.qa_failure_summary import summarize_qa_failures
from app.schemas.qa_suite import APIStep, BrowserStep, QAScript, QASuite
from app.services.qa_assets import RUNNER_VERSION
from app.services.qa_verification import QAVerificationState, resolvable_defect_ids

_MAX_RECENT_READ_CHARS = 24_000
_MAX_READ_RESULT_CHARS = 3_000
_MAX_ARTIFACT_READ_RESULT_CHARS = 6_000
_MAX_SCRIPT_STEPS = 240
_READ_TOOLS = frozenset({"read_artifact", "list_files", "read_file", "search_code"})
_PRIVATE_SELECTOR = re.compile(r"@|password|token|bearer|\d{10,}", re.IGNORECASE)
_PRIVATE_LITERAL = re.compile(
    r"(?i)(['\"]?\b(?:password|token|secret|authorization|api[_-]?key)\b['\"]?\s*[=:]\s*['\"])[^'\"\r\n]+(['\"])"
)
_PRIVATE_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PRIVATE_NUMBER = re.compile(r"\b\d{10,}\b")
_PRIVATE_MARKER = re.compile(r"\bSECRET_[A-Z0-9_]+\b")
_PRIVATE_BEARER = re.compile(r"(?i)(\bBearer\s+)\S+")
_SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SAFE_MUTATION_ERRORS = frozenset(
    {
        "测试资产已更新；须提供准确 expected_suite_hash",
        "指定用例没有独立脚本",
        "脚本步骤索引无效",
        "每次只能修改一个定位器/路径或插入一个等待步骤",
        "只能在浏览器步骤后插入等待步骤",
        "该步骤没有可修改的浏览器定位器",
        "该步骤不允许修改路径",
        "不能删除操作或降低断言；请提交 test_challenges",
        "不能删除场景或修改业务预期；请提交 test_challenges",
        "测试场景必须覆盖全部获批验收条件",
        "独立测试资产修订预算已用尽",
        "请先设计场景，或测试资产修订预算已用尽",
        "独立场景尚未设计，或执行预算已用尽",
        "当前源码、测试资产和执行器已有独立执行证据；"
        "请先修订脚本生成新 suite_hash，不能重复启动同一快照",
        "该调用未执行；只读工具可批量调用，状态变更请根据前一个结果单独继续。",
        "标准争议报告不能引用不存在的测试执行",
        "未设计独立套件时只能提交标准争议报告",
        "质量结论没有引用当前 suite_hash",
        "质量结论没有引用当前测试执行",
        "测试报告引用的不是本轮准确代码结果",
        "测试报告引用的不是本轮冻结验收标准",
        "报告必须包含平台冻结的独立测试场景，不能从需求结论合成测试结果",
        "测试报告没有绑定当前独立测试资产版本",
        "当前测试资产须使用最新独立执行器重新执行",
        "逐项测试结果必须准确引用运行器证据；smoke、截图和模型结论不能替代",
        "交付前必须在当前源码和测试版本完成全套回归",
        "需求结论必须来自关联独立用例的实际状态",
        "测试报告必须逐条覆盖 PRD 验收条件",
        "提交测试报告前必须运行完整 all 检查",
        "提交测试报告前必须生成桌面端与移动端截图",
        "测试报告必须记录本轮全部实际检查",
        "测试报告中的缺陷必须来自本轮独立记录",
        "验收失败时必须记录至少一个缺陷",
    }
)


def _failure_category(evidence: object) -> str | None:
    if not isinstance(evidence, str):
        return None
    lower = evidence.lower()
    if "strict mode violation" in lower:
        return "non_unique_browser_locator"
    if "timeout" in lower and ("navigate" in lower or "locator" in lower):
        return "browser_transition_timeout_check_script_synchronization"
    if "timeout" in lower:
        return "timeout"
    if "assert" in lower:
        return "assertion_mismatch"
    if "connection" in lower or "refused" in lower:
        return "connection_failure"
    return "inspect_runner_evidence_before_classifying"


def _safe_selector(selector: object) -> str | None:
    if not isinstance(selector, str):
        return None
    if len(selector) > 120 or _PRIVATE_SELECTOR.search(selector):
        return "<redacted; inspect the specific script if needed>"
    return selector


def _step_identity(step: APIStep | BrowserStep) -> dict[str, str]:
    if isinstance(step, BrowserStep):
        return {"kind": "browser", "action": step.action}
    return {"kind": "api", "action": step.method}


def _login_barrier_after_step(script: QAScript, failed_index: int) -> int | None:
    """Suggest a passive wait only near a login click followed by another route."""

    for click_index in (failed_index - 1, failed_index - 2):
        if not 0 <= click_index < len(script.steps) - 1:
            continue
        click, destination = script.steps[click_index : click_index + 2]
        if not (
            isinstance(click, BrowserStep)
            and click.action == "click"
            and isinstance(destination, BrowserStep)
            and destination.action == "navigate"
        ):
            continue
        earlier_routes = [
            step.path
            for step in script.steps[:click_index]
            if isinstance(step, BrowserStep) and step.action == "navigate"
        ]
        if not earlier_routes:
            continue
        previous_route = earlier_routes[-1] or ""
        login_route = r"/(?:login|signin|sign-in|log-in)(?:[/?#]|$)"
        if re.search(login_route, previous_route, re.I) and not re.search(
            login_route, destination.path or "", re.I
        ):
            return click_index
    return None


def _failed_step_details(row: dict[str, Any], script: QAScript) -> dict[str, Any]:
    """Trusted runner observations count completed steps; the next step failed."""

    if row.get("status") not in {"failed", "blocked"} or row.get("failure_kind") not in {
        "product",
        "script",
    }:
        return {}
    observations = row.get("observations")
    if not isinstance(observations, list) or not all(
        isinstance(item, dict) for item in observations
    ):
        return {}
    failed_index = len(observations)
    if failed_index >= len(script.steps):
        return {}
    failed = script.steps[failed_index]
    details: dict[str, Any] = {
        "failed_step_index": failed_index,
        "failed_step": _step_identity(failed),
    }
    if isinstance(failed, BrowserStep):
        details["failed_step"]["selector"] = _safe_selector(failed.selector)
    if failed_index:
        details["previous_step"] = _step_identity(script.steps[failed_index - 1])
    barrier = _login_barrier_after_step(script, failed_index)
    if barrier is not None:
        details["suggested_login_barrier_after_step_index"] = barrier
    return details


def _next_action_guidance(
    *,
    suite_exists: bool,
    suite_hash: str | None,
    latest: dict[str, Any] | None,
    results: list[dict[str, Any]],
    unresolved_script_test_ids: list[str],
    unresolved_login_transition_test_ids: list[str],
) -> str:
    if not suite_exists:
        return "先调用 design_test_cases；冻结验收计划已在本消息中，无需遍历源码。"
    if latest is None:
        return "当前套件尚未执行；完成必要脚本后运行独立验收。"
    if unresolved_script_test_ids or unresolved_login_transition_test_ids:
        details = []
        if any(row.get("diagnostic_category") == "non_unique_browser_locator" for row in results):
            details.append("多匹配定位器应按失败步骤改为唯一 selector")
        if unresolved_login_transition_test_ids:
            details.append(
                "登录点击后立刻导航的用例在首个页面断言超时："
                + "、".join(unresolved_login_transition_test_ids)
                + "；逐项按 latest_execution.results 中的 "
                "suggested_login_barrier_after_step_index，"
                "用 adapt_test_script 在登录点击后插入 wait_for_visible，"
                "等待真实登录成功的可见元素，保留原业务断言"
            )
        elif any("suggested_login_barrier_after_step_index" in row for row in results):
            details.append("登录点击后可在建议索引插入 wait_for_visible 同步状态")
        return (
            "上一轮独立执行仍有待安全适配的脚本。"
            + (
                "script 阻断且脚本未改：" + "、".join(unresolved_script_test_ids) + "。"
                if unresolved_script_test_ids
                else ""
            )
            + "先安全适配并重跑：按各自失败步骤逐个处理；"
            + ("；".join(details) + "。" if details else "")
            + "全部已知脚本阻断处理后，继续处理全部已知登录同步问题，"
            "再用新 suite_hash 运行一次完整 run_acceptance_tests。"
            "勿仅因 suite_hash 已变化就立即重跑，勿将混合结果直接提交 failed 源码结论；"
            "同步提示不替代独立执行对产品缺陷的判断。"
        )
    if latest.get("suite_hash") != suite_hash:
        return "当前 suite_hash 尚无独立执行证据；运行一次 run_acceptance_tests。"
    if latest.get("runner_version") != RUNNER_VERSION:
        return "既有执行器版本已过期；当前 suite_hash 需以新执行器完整重跑独立验收。"
    if any(row["status"] != "passed" for row in results):
        categories = {row.get("diagnostic_category") for row in results}
        advice = []
        script_blocked = any(
            row.get("status") == "blocked" and row.get("failure_kind") == "script"
            for row in results
        )
        if "non_unique_browser_locator" in categories:
            advice.append("多匹配定位器应按失败步骤改为唯一 selector")
        if any("suggested_login_barrier_after_step_index" in row for row in results):
            advice.append(
                "登录点击后紧接受保护页导航时，在建议索引后插入 wait_for_visible 同步状态"
            )
        elif "browser_transition_timeout_check_script_synchronization" in categories:
            advice.append("浏览器跳转超时先检查相邻步骤的同步")
        if "assertion_mismatch" in categories:
            advice.append("断言不符时核对冻结预期与实际产品行为，不可降低断言")
        if not advice:
            advice.append("按失败步骤和诊断类别判断脚本、环境或产品缺陷")
        if script_blocked:
            advice.append(
                "当前仍有 script 阻断：先安全适配并重跑，勿将混合结果直接提交 failed 源码结论；"
                "确实无法修复脚本时才提交 blocked 报告"
            )
        return (
            "；".join(advice)
            + "。若是脚本问题，定向适配后用新 suite_hash 重跑；勿重复执行同一快照。"
        )
    return "使用当前 suite_hash 和已有检查证据完成剩余 QA 步骤。"


def _executed_suite(state: QAVerificationState, latest: dict[str, Any] | None) -> QASuite | None:
    if latest is None or any(
        (
            latest.get("code_item_id") != state.code_item_id,
            latest.get("source_hash") != state.code_source_hash,
            latest.get("runner_version") != RUNNER_VERSION,
        )
    ):
        return None
    return next(
        (suite for suite in state.suite_versions if suite.digest() == latest.get("suite_hash")),
        None,
    )


def _unresolved_script_test_ids(
    current: QASuite | None,
    executed: QASuite | None,
    latest: dict[str, Any] | None,
) -> list[str]:
    if current is None or executed is None or latest is None:
        return []
    rows = latest.get("results")
    if not isinstance(rows, list):
        return []
    unresolved = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("status") not in {"blocked", "failed"}:
            continue
        test_id = row.get("test_id")
        if row.get("failure_kind") != "script" or not isinstance(test_id, str):
            continue
        current_script = current.scripts.get(test_id)
        executed_script = executed.scripts.get(test_id)
        if current_script is None or current_script == executed_script:
            unresolved.add(test_id)
    return sorted(unresolved)


def _unresolved_login_transition_test_ids(
    current: QASuite | None,
    executed: QASuite | None,
    latest: dict[str, Any] | None,
) -> list[str]:
    """Find unchanged login click→route races from exact runner browser timeouts.

    The timeout alone does not prove a script defect. This only withholds an
    expensive full rerun while the same unsynchronized transition remains.
    A later runner execution still decides whether the app works.
    """

    if current is None or executed is None or latest is None:
        return []
    rows = latest.get("results")
    if not isinstance(rows, list):
        return []
    unresolved = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "failed":
            continue
        if row.get("failure_kind") != "product":
            continue
        evidence = row.get("evidence")
        if not isinstance(evidence, str) or not evidence.startswith("TimeoutError:"):
            continue
        test_id = row.get("test_id")
        if not isinstance(test_id, str):
            continue
        old_script = executed.scripts.get(test_id)
        new_script = current.scripts.get(test_id)
        observations = row.get("observations")
        if old_script is None or new_script is None or not isinstance(observations, list):
            continue
        failed_index = len(observations)
        if failed_index < 2 or failed_index >= len(old_script.steps):
            continue
        failed = old_script.steps[failed_index]
        if not isinstance(failed, BrowserStep) or not failed.action.startswith("expect_"):
            continue
        if any(
            not isinstance(observation, dict)
            or observation.get("kind") != step.kind
            or (isinstance(step, BrowserStep) and observation.get("action") != step.action)
            for step, observation in zip(old_script.steps, observations, strict=False)
        ):
            continue
        click_index = _login_barrier_after_step(old_script, failed_index)
        if click_index != failed_index - 2:
            continue
        login_path = next(
            (
                step.path
                for step in reversed(old_script.steps[:click_index])
                if isinstance(step, BrowserStep) and step.action == "navigate"
            ),
            None,
        )
        click = old_script.steps[click_index]
        destination = old_script.steps[click_index + 1]
        if not isinstance(click, BrowserStep) or not isinstance(destination, BrowserStep):
            continue
        transition = (login_path, click.selector, destination.path)
        for index in range(len(new_script.steps) - 1):
            current_click, current_destination = new_script.steps[index : index + 2]
            if not (
                isinstance(current_click, BrowserStep)
                and current_click.action == "click"
                and isinstance(current_destination, BrowserStep)
                and current_destination.action == "navigate"
            ):
                continue
            current_login_path = next(
                (
                    step.path
                    for step in reversed(new_script.steps[:index])
                    if isinstance(step, BrowserStep) and step.action == "navigate"
                ),
                None,
            )
            if (current_login_path, current_click.selector, current_destination.path) == transition:
                unresolved.add(test_id)
                break
    return sorted(unresolved)


def unresolved_login_transition_test_ids(state: QAVerificationState) -> list[str]:
    """Shared projection for QA guidance and the trusted rerun gate."""

    current = state.suite_versions[-1] if state.suite_versions else None
    latest = state.executions[-1] if state.executions else None
    executed = _executed_suite(state, latest)
    return _unresolved_login_transition_test_ids(current, executed, latest)


def qa_model_progress(state: QAVerificationState) -> dict[str, Any]:
    """Expose identities and actionable status, never raw runner responses or bodies."""

    suite = state.suite_versions[-1] if state.suite_versions else None
    suite_hash = suite.digest() if suite is not None else None
    latest = state.executions[-1] if state.executions else None
    rows = latest.get("results", []) if latest is not None else []
    executed_suite = _executed_suite(state, latest)
    unresolved_script_test_ids = _unresolved_script_test_ids(suite, executed_suite, latest)
    unresolved_login_transition_ids = _unresolved_login_transition_test_ids(
        suite, executed_suite, latest
    )
    results = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        result = {
            "test_id": row.get("test_id"),
            "status": row.get("status"),
            "failure_kind": row.get("failure_kind"),
            "diagnostic_category": _failure_category(row.get("evidence"))
            if row.get("status") != "passed"
            else None,
        }
        test_id = row.get("test_id")
        if suite is not None and executed_suite is not None and isinstance(test_id, str):
            current_script = suite.scripts.get(test_id)
            if current_script is not None and current_script == executed_suite.scripts.get(test_id):
                result.update(_failed_step_details(row, current_script))
        results.append(result)
    targets = {
        row["test_id"]
        for row in results
        if row["status"] in {"failed", "blocked", "not_run"} and isinstance(row["test_id"], str)
    }
    script_steps: dict[str, list[dict[str, Any]]] = {}
    if suite is not None:
        for test_id in sorted(targets):
            script = suite.scripts.get(test_id)
            if script is None:
                continue
            browser = []
            for index, step in enumerate(script.steps):
                if step.kind != "browser":
                    continue
                browser.append(
                    {
                        "step_index": index,
                        "action": step.action,
                        "selector": _safe_selector(step.selector),
                    }
                )
                if sum(map(len, script_steps.values())) + len(browser) >= _MAX_SCRIPT_STEPS:
                    break
            script_steps[test_id] = browser
            if sum(map(len, script_steps.values())) >= _MAX_SCRIPT_STEPS:
                break
    previous = [
        {
            "execution_id": item.get("execution_id"),
            "suite_hash": item.get("suite_hash"),
            "runner_version": item.get("runner_version"),
        }
        for item in state.executions
    ]
    checks = {
        check_id: {"ok": result.ok, "error_code": result.error_code}
        for check_id, result in state.checks.items()
    }
    latest_matches_source = (
        latest is not None
        and latest.get("code_item_id") == state.code_item_id
        and latest.get("source_hash") == state.code_source_hash
        and executed_suite is not None
    )
    source_bound_checks = {
        check_id: result
        for check_id, result in state.checks.items()
        if result.data.get("source_hash") in {None, state.code_source_hash}
    }
    return {
        "code_item_id": state.code_item_id,
        "source_hash": state.code_source_hash,
        "test_plan_item_id": state.acceptance_test_plan_item_id,
        "test_hash": state.acceptance_test_hash,
        "tool_turns": state.tool_turns,
        "tool_budget": state.tool_budget,
        "remaining_tool_turns": max(0, state.tool_budget - state.tool_turns),
        "execution_budget": state.execution_budget,
        "remaining_execution_runs": max(0, state.execution_budget - len(state.executions)),
        "latest_suite": {
            "suite_hash": suite_hash,
            "revision": suite.revision if suite is not None else None,
            "test_ids": [case.test_id for case in suite.scenarios] if suite is not None else [],
            "scripted_test_ids": list(suite.scripts) if suite is not None else [],
        },
        "execution_history": previous,
        "unresolved_script_test_ids": unresolved_script_test_ids,
        "unresolved_login_transition_test_ids": unresolved_login_transition_ids,
        "latest_execution": {
            "execution_id": latest.get("execution_id"),
            "suite_hash": latest.get("suite_hash"),
            "current_suite_evaluated": latest.get("suite_hash") == suite_hash,
            "current_runner_evaluated": latest.get("runner_version") == RUNNER_VERSION,
            "results": results,
        }
        if latest is not None
        else None,
        "browser_steps_for_nonpassing_tests": script_steps,
        "checks": checks,
        "failure_facts": summarize_qa_failures(
            latest if latest_matches_source else None,
            executed_suite,
            source_bound_checks,
        ),
        "active_defect_ids": list(state.active_defects),
        "resolved_defect_ids": list(state.defect_resolutions),
        "resolvable_defect_ids": resolvable_defect_ids(state),
        "next_action_guidance": _next_action_guidance(
            suite_exists=suite is not None,
            suite_hash=suite_hash,
            latest=latest,
            results=results,
            unresolved_script_test_ids=unresolved_script_test_ids,
            unresolved_login_transition_test_ids=unresolved_login_transition_ids,
        ),
    }


def _redact_read_text(value: str) -> str:
    """Keep source structure while dropping common literal credentials and PII."""

    value = _PRIVATE_LITERAL.sub(r"\1<redacted>\2", value)
    value = _PRIVATE_EMAIL.sub("<redacted-email>", value)
    value = _PRIVATE_NUMBER.sub("<redacted-number>", value)
    value = _PRIVATE_MARKER.sub("<redacted-secret>", value)
    return _PRIVATE_BEARER.sub(r"\1<redacted>", value)


def _redact_read_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_read_text(value)
    if isinstance(value, list):
        return [_redact_read_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_read_value(item) for key, item in value.items()}
    return value


def _private_search_query(value: str) -> bool:
    return any(
        pattern.search(value)
        for pattern in (
            _PRIVATE_LITERAL,
            _PRIVATE_EMAIL,
            _PRIVATE_NUMBER,
            _PRIVATE_MARKER,
            _PRIVATE_BEARER,
        )
    )


def _safe_read_result(name: str, raw: str) -> str | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    safe_data: dict[str, Any] = {}
    if name == "read_artifact":
        # Initial QA still needs frozen product intent to design independent
        # scenarios. Only the four known artifact shapes may enter the cache.
        for key in (
            "app_spec",
            "system_design",
            "acceptance_test_plan",
            "code_item_id",
            "source_hash",
            "test_plan_item_id",
            "test_hash",
        ):
            if key in data:
                safe_data[key] = _redact_read_value(data[key])
    elif name == "read_file":
        for key in ("path", "start_line", "end_line", "line_count", "content_hash"):
            if key in data:
                value = data[key]
                safe_data[key] = _redact_read_text(value) if isinstance(value, str) else value
        content = data.get("content")
        if isinstance(content, str):
            safe_data["content"] = _redact_read_text(content)
    elif name == "list_files":
        files = data.get("files")
        if isinstance(files, list):
            safe_data["files"] = [
                {
                    "path": _redact_read_text(str(row.get("path") or "")),
                    "size_bytes": row.get("size_bytes"),
                }
                for row in files
                if isinstance(row, dict)
            ]
        safe_data["truncated"] = data.get("truncated")
    elif name == "search_code":
        safe_data["query"] = _redact_read_text(str(data.get("query") or ""))
        hits = data.get("hits")
        if isinstance(hits, list):
            safe_data["hits"] = [
                {
                    "path": _redact_read_text(str(row.get("path") or "")),
                    "line": row.get("line"),
                    "text": _redact_read_text(str(row.get("text") or "")),
                }
                for row in hits
                if isinstance(row, dict)
            ]
        safe_data["truncated"] = data.get("truncated")
    result = {
        "ok": payload.get("ok"),
        "summary": _redact_read_text(str(payload.get("summary") or "")),
        "data": safe_data,
    }
    result_limit = (
        _MAX_ARTIFACT_READ_RESULT_CHARS if name == "read_artifact" else _MAX_READ_RESULT_CHARS
    )
    serialized = json.dumps(result, ensure_ascii=False)
    if len(serialized) <= result_limit:
        return serialized
    result["data"] = {
        "path": safe_data.get("path"),
        "start_line": safe_data.get("start_line"),
        "end_line": safe_data.get("end_line"),
        "content_hash": safe_data.get("content_hash"),
        "excerpt": serialized[: result_limit - 600],
    }
    result["truncated"] = True
    return json.dumps(result, ensure_ascii=False)


def _recent_read_exchange(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain recent unique reads across batches, even after mutation turns."""

    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    size = 0
    for index in range(len(history) - 1, 1, -1):
        message = history[index]
        calls = message.get("tool_calls")
        if message.get("role") != "assistant" or not isinstance(calls, list) or not calls:
            continue
        rows = history[index + 1 : index + 1 + len(calls)]
        if len(rows) != len(calls) or any(row.get("role") != "tool" for row in rows):
            continue
        for call, row in reversed(list(zip(calls, rows, strict=True))):
            if not isinstance(call, dict) or not isinstance(row, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if name not in _READ_TOOLS or row.get("tool_call_id") != call.get("id"):
                continue
            raw_args = function.get("arguments")
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else None
            except (TypeError, ValueError):
                continue
            if not isinstance(arguments, dict):
                continue
            # Search terms may themselves be private. They need not enter the
            # durable model projection for the model to make forward progress.
            if name == "search_code" and _private_search_query(str(arguments.get("query", ""))):
                continue
            key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
            if key in seen:
                continue
            seen.add(key)
            raw = row.get("content")
            if not isinstance(raw, str):
                continue
            content = _safe_read_result(name, raw)
            if content is None:
                continue
            safe_arguments = {
                argument: _redact_read_text(value) if isinstance(value, str) else value
                for argument, value in arguments.items()
            }
            safe_call = {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(safe_arguments, sort_keys=True, ensure_ascii=False),
                },
            }
            safe_row = {
                "role": "tool",
                "tool_call_id": call["id"],
                "name": name,
                "content": content,
            }
            assistant = {
                "role": "assistant",
                "content": "",
                "reasoning_content": "",
                "tool_calls": [safe_call],
            }
            entry_size = len(json.dumps(assistant, ensure_ascii=False)) + len(
                json.dumps(safe_row, ensure_ascii=False)
            )
            if size + entry_size > _MAX_RECENT_READ_CHARS:
                continue
            selected.append((safe_call, safe_row))
            size += entry_size
    messages: list[dict[str, Any]] = []
    for call, row in reversed(selected):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "",
                "tool_calls": [call],
            }
        )
        messages.append(row)
    return messages


def _safe_mutation_arguments(
    name: str, arguments: object, state: QAVerificationState
) -> dict[str, Any]:
    """Only expose identity/index fields; scripts, selectors and bodies may contain secrets."""

    if not isinstance(arguments, dict):
        return {}
    result: dict[str, Any] = {}
    suite = state.suite_versions[-1] if state.suite_versions else None
    test_id = arguments.get("test_id")
    if (
        suite is not None
        and isinstance(test_id, str)
        and test_id in {case.test_id for case in suite.scenarios}
    ):
        result["test_id"] = test_id
    execution_id = arguments.get("execution_id")
    if isinstance(execution_id, str) and execution_id in {
        row.get("execution_id") for row in state.executions
    }:
        result["execution_id"] = execution_id
    value = arguments.get("expected_suite_hash")
    if isinstance(value, str):
        result["expected_suite_hash_matches_latest"] = suite is not None and value == suite.digest()
    if name == "report_decision":
        report_suite_hash = arguments.get("suite_hash")
        if isinstance(report_suite_hash, str):
            result["suite_hash_matches_latest"] = (
                suite is not None and report_suite_hash == suite.digest()
            )
        report_execution_id = arguments.get("execution_id")
        if isinstance(report_execution_id, str):
            result["execution_id_matches_latest"] = bool(
                state.executions and report_execution_id == state.executions[-1].get("execution_id")
            )
    index = arguments.get("step_index")
    if type(index) is int and 0 <= index <= 10_000:
        result["step_index"] = index
    if name == "adapt_test_script":
        result["edit_fields"] = [
            key
            for key in ("selector", "path", "insert_wait_for_visible_selector")
            if key in arguments
        ]
    check_id = arguments.get("check_id")
    if (
        name == "run_check"
        and isinstance(check_id, str)
        and check_id
        in {
            "database",
            "backend",
            "frontend",
            "all",
        }
    ):
        result["check_id"] = check_id
    if name == "write_test_report":
        decision = arguments.get("report_decision")
        if isinstance(decision, dict):
            result.update(_safe_mutation_arguments("report_decision", decision, state))
            conclusion = decision.get("quality_conclusion")
            if isinstance(conclusion, str) and conclusion in {"passed", "failed", "blocked"}:
                result["quality_conclusion"] = conclusion
    return result


def _recent_mutation_failure(
    history: list[dict[str, Any]], state: QAVerificationState
) -> dict[str, Any] | None:
    """Project the last rejected mutation without replaying private tool arguments."""

    for index in range(len(history) - 1, 1, -1):
        message = history[index]
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        calls = message["tool_calls"]
        if not isinstance(calls, list):
            continue
        rows = history[index + 1 : index + 1 + len(calls)]
        if len(rows) != len(calls) or any(row.get("role") != "tool" for row in rows):
            continue
        mutations = []
        for call, row in zip(calls, rows, strict=True):
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if isinstance(function, dict) and function.get("name") not in _READ_TOOLS:
                mutations.append((call, row))
        if not mutations:
            continue
        call, row = mutations[-1]
        for candidate_call, candidate_row in mutations:
            try:
                candidate = json.loads(candidate_row.get("content", ""))
            except (TypeError, ValueError):
                continue
            if isinstance(candidate, dict) and candidate.get("error_code") != "TOOL_CALL_DEFERRED":
                call, row = candidate_call, candidate_row
                break
        if row.get("tool_call_id") != call.get("id"):
            return None
        try:
            observation = json.loads(row.get("content", ""))
        except (TypeError, ValueError):
            return None
        if not isinstance(observation, dict) or observation.get("ok") is not False:
            return None
        function = call.get("function", {})
        name = function.get("name")
        if not isinstance(name, str) or not _SAFE_ID.fullmatch(name):
            return None
        try:
            arguments = json.loads(function.get("arguments", "{}"))
        except (TypeError, ValueError):
            arguments = {}
        error_code = observation.get("error_code")
        summary = observation.get("summary")
        return {
            "name": name,
            "arguments": _safe_mutation_arguments(name, arguments, state),
            "error_code": error_code
            if isinstance(error_code, str) and _ERROR_CODE.fullmatch(error_code)
            else "TOOL_REJECTED",
            "summary": summary
            if isinstance(summary, str) and summary in _SAFE_MUTATION_ERRORS
            else "工具拒绝了本次调用；请检查参数、步骤类型和当前 suite_hash。",
        }
    return None


def qa_model_messages(
    *,
    system_message: dict[str, Any],
    fixed_context: dict[str, Any],
    state: QAVerificationState,
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project fresh bounded context from checkpoint, never replay full QA history."""

    context = {**fixed_context, "resumed_progress": qa_model_progress(state)}
    failure = _recent_mutation_failure(history, state)
    if failure is not None:
        context["last_failed_mutation"] = failure
    messages = [
        dict(system_message),
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]
    read_memory = _recent_read_exchange(history)
    messages.extend(read_memory)
    if read_memory:
        read_guidance = (
            "以上只读结果包含本轮近期已读取的不同文件片段；"
            "先利用已有内容，只补读尚未覆盖的范围，勿重复调用相同工具参数。"
        )
        if not state.suite_versions and state.tool_turns >= 12:
            read_guidance += "冻结验收计划已提供，请先调用 design_test_cases。"
        messages.append(
            {
                "role": "user",
                "content": read_guidance,
            }
        )
    if history and history[-1].get("role") == "user" and len(history) > 2:
        content = history[-1].get("content")
        if isinstance(content, str):
            messages.append({"role": "user", "content": content[:700]})
    return messages
