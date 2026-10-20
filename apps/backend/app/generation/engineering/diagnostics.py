"""Evidence adapters and bounded correction state, without I/O or model decisions."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.schemas.agent_action import ToolExecutionResult
from app.schemas.engineering_diagnostic import EngineeringDiagnostic
from app.schemas.test_report import TestReport, VerificationStatus
from app.tools.check_diagnostics import analyze_check_failure, repair_verification

MAX_IDENTICAL_TOOL_FAILURES = 3


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def feedback(result: ToolExecutionResult, *, check_id: str | None = None) -> ToolExecutionResult:
    """Add normalized guidance; retain the original result and raw evidence."""
    if result.ok:
        return result
    raw = result.data.get("diagnostic")
    if isinstance(raw, dict):
        diagnostic = EngineeringDiagnostic.model_validate(raw)
    else:
        code = result.error_code or "TOOL_REJECTED"
        check_id = check_id or result.data.get("check_id")
        is_check = check_id is not None or result.name in {
            "run_check",
            "complete_work_item",
            "check_environment",
        }
        analysis = analyze_check_failure(result) if is_check else None
        diagnostic = EngineeringDiagnostic(
            category="database" if check_id == "database" else "build" if is_check else "tool",
            disposition="repair",
            code=code,
            summary=result.summary,
            issues=[d.to_payload() for d in analysis.diagnostics] if analysis else [],
            verification=f"run_check(check_id={check_id})"
            if check_id
            else f"重新执行 {result.name}",
            source_hash=result.data.get("source_hash"),
        )
        if analysis and any(d.kind == "api" for d in analysis.diagnostics):
            diagnostic.category = "api"
        if result.data.get("validation_errors"):
            diagnostic.issues = list(result.data["validation_errors"])
        if not diagnostic.issues:
            diagnostic.issues = [
                {
                    "code": code,
                    "location": {
                        key: result.arguments[key]
                        for key in ("path", "check_id", "work_item_id")
                        if key in result.arguments
                    },
                    "actual": analysis.excerpt if analysis else result.summary,
                    "evidence": str(result.data.get("output") or result.summary),
                    "cause": "unknown",
                }
            ]
        if code in {"CHECK_ENVIRONMENT_UNAVAILABLE", "CHECK_TIMEOUT"}:
            diagnostic.category = "environment"
            diagnostic.disposition = "restore_environment"
            diagnostic.verification = (
                "恢复环境或检查超时原因后重新执行原检查；不能靠删除功能或修改验收通过。"
            )
        elif code in {"SOURCE_CHANGED", "WRITE_CONFLICT"}:
            diagnostic.disposition = "refresh_evidence"
            diagnostic.verification = "重新读取当前源码与 hash，再修改或执行原检查。"
        if analysis:
            diagnostic.fingerprint = analysis.fingerprint
        if is_check:
            plan = repair_verification(result)
            diagnostic.verification = (
                (
                    "先 generate_api_client 更新接口契约，再请求 complete_work_item"
                    if plan["tool"] == "generate_api_client"
                    else f"修改后先 run_check(check_id={plan['check_id']}) 定向复验，"
                    "再 complete_work_item；"
                    "局部通过不解除原完整检查失败，也不代表业务验收通过"
                )
                if diagnostic.disposition == "repair"
                else diagnostic.verification
            )
    if result.name == "model_response":
        diagnostic.verification = (
            "依据本轮 available_tools 提交合法工具调用；写入或状态变更单独调用。"
        )
    if not diagnostic.fingerprint:
        diagnostic.fingerprint = _digest([diagnostic.category, diagnostic.code, diagnostic.issues])
    return result.model_copy(
        update={
            "data": {
                **result.data,
                "diagnostic": diagnostic.model_dump(mode="json"),
            }
        }
    )


def _scope(result: ToolExecutionResult, check_id: str | None) -> str:
    if check_id:
        return f"check:{check_id}"
    if result.name == "update_delivery_plan":
        return "plan"
    return f"tool:{result.name}:{result.arguments.get('path', '')}"


def record_feedback(
    checkpoint: dict[str, Any], result: ToolExecutionResult, *, check_id: str | None = None
) -> ToolExecutionResult:
    """A successful unrelated tool never resolves an outstanding failure."""
    check_id = check_id or result.data.get("check_id")
    result = feedback(result, check_id=check_id)
    scope = _scope(result, check_id)
    states = checkpoint.setdefault("diagnostic_state", {})
    if result.ok:
        if (scope in states or check_id) and (not check_id or result.data.get("source_hash")):
            states.setdefault(scope, {}).update(
                status="resolved",
                verification_source_hash=result.data.get("source_hash"),
                verification_status="current",
                repeated_attempts=0,
            )
        return result
    diagnostic = result.data["diagnostic"]
    arguments = dict(result.arguments)
    if result.name == "update_delivery_plan":
        arguments.pop("reason", None)  # Cosmetic explanations are not a different repair.
        items = arguments.get("items")
        # This path also handles schema-rejected calls; never assume items parsed.
        if isinstance(items, list) and all(isinstance(item, dict) for item in items):
            arguments["items"] = sorted(items, key=lambda item: str(item.get("id", "")))
    if check_id:
        arguments = {"check_id": check_id}
    identity = _digest(
        [arguments, result.data.get("available_tools")]
        if result.name == "model_response"
        else [arguments, diagnostic.get("source_hash") or checkpoint.get("source_version", 0)]
    )
    previous = states.get(scope, {})
    repeated = (
        int(previous.get("repeated_attempts", 0)) + 1
        if previous.get("input_fingerprint") == identity
        and previous.get("diagnostic", {}).get("fingerprint") == diagnostic["fingerprint"]
        and previous.get("status") not in {"resolved", "superseded"}
        else 1
    )
    states[scope] = {
        "diagnostic": diagnostic,
        "status": "open",
        "input_fingerprint": identity,
        "repeated_attempts": repeated,
        "source_version": checkpoint.get("source_version", 0),
        "verification_status": "current",
    }
    if result.name == "model_response":
        states[scope]["available_tools"] = result.data.get("available_tools")
    # Store distinct raw feedback once. Repeated calls remain in the activity stream.
    history = checkpoint.setdefault("diagnostic_history", [])
    evidence_id = _digest([scope, identity, diagnostic["fingerprint"]])
    if not any(entry["evidence_id"] == evidence_id for entry in history):
        history.append(
            {
                "evidence_id": evidence_id,
                "scope": scope,
                "model_turn": checkpoint.get("model_turns", 0),
                "result": result.model_dump(mode="json"),
            }
        )
    guidance = {
        "repeated_attempts": repeated,
        "instruction": "相同输入和错误再次出现；针对 diagnostic 修改或报告具体阻碍，不要原样重交。"
        if repeated > 1
        else "按 diagnostic 定位后修改，再执行 verification；模型声明不能代替检查。",
    }
    return result.model_copy(update={"data": {**result.data, "correction": guidance}})


def reconcile_protocol_diagnostic(checkpoint: dict[str, Any], available: list[str]) -> None:
    """Protocol refusals apply only to the capabilities offered for that response."""
    state = checkpoint.get("diagnostic_state", {}).get("tool:model_response:")
    if state and state.get("status") == "open" and state.get("available_tools") != available:
        state.update(status="superseded", repeated_attempts=0)


def resolve_protocol_diagnostic(checkpoint: dict[str, Any]) -> None:
    """A validated model tool call resolves response format/selection, not tool failures."""
    state = checkpoint.get("diagnostic_state", {}).get("tool:model_response:")
    if state and state.get("status") == "open":
        state.update(status="resolved", repeated_attempts=0)


def invalidate_source_evidence(checkpoint: dict[str, Any]) -> None:
    checkpoint["source_version"] = int(checkpoint.get("source_version", 0)) + 1
    checkpoint["check_cache"] = {}
    for scope, state in checkpoint.get("diagnostic_state", {}).items():
        if scope.startswith("check:"):
            state["verification_status"] = "needs_verification"
            if state.get("status") != "resolved":
                state["status"] = "needs_verification"
    repair = checkpoint.get("repair_context")
    if isinstance(repair, dict):
        repair["requires_recheck"] = True


def check_verification(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    """Last outcome and current validity are separate facts, never revived errors."""
    return [
        {
            "check_id": scope.removeprefix("check:"),
            "last_outcome": "passed" if state.get("status") == "resolved" else "failed",
            "source_hash": state.get("verification_source_hash")
            if state.get("status") == "resolved"
            else state.get("diagnostic", {}).get("source_hash"),
            "verification_status": state.get("verification_status", "current"),
        }
        for scope, state in checkpoint.get("diagnostic_state", {}).items()
        if scope.startswith("check:")
    ]


def active_diagnostics(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    """Model projection; raw outputs and repeated plans stay in the checkpoint."""
    rows = []
    for scope, state in checkpoint.get("diagnostic_state", {}).items():
        if state.get("status") in {"resolved", "superseded"}:
            continue
        rows.append(
            {
                "scope": scope,
                "historical": state.get("verification_status") == "needs_verification",
                **{
                    key: state.get(key)
                    for key in ("status", "diagnostic", "repeated_attempts", "source_version")
                },
            }
        )
    return rows


def acceptance_feedback(report: TestReport) -> EngineeringDiagnostic:
    """QA owns resolution. Passing engineering smoke cannot close this evidence."""
    issues: list[dict[str, Any]] = [
        {
            "code": "ACCEPTANCE_DEFECT",
            "defect_id": d.defect_id,
            "requirement_ids": d.related_requirement_ids,
            "test_ids": d.related_test_ids,
            "actual": d.description,
            "evidence": d.evidence,
        }
        for d in report.defects
    ]
    issues.extend(
        {
            "code": "ACCEPTANCE_TEST",
            "test_id": t.test_id,
            "failure_kind": t.failure_kind,
            "evidence": t.evidence,
        }
        for t in report.test_results
        if t.status != VerificationStatus.PASSED
    )
    if not issues:
        issues = [
            {
                "code": "ACCEPTANCE_REQUIREMENT",
                "requirement_id": r.requirement_id,
                "evidence": r.evidence,
            }
            for r in report.requirement_results
            if r.status != VerificationStatus.PASSED
        ]
    issues.extend(
        {"code": "ACCEPTANCE_CHECK", "check_id": c.check_id, "evidence": c.evidence}
        for c in report.check_results
        if c.status != VerificationStatus.PASSED
    )
    issues.extend(
        {
            "code": "ACCEPTANCE_CONFLICT",
            "challenge_id": c.challenge_id,
            "test_ids": c.test_ids,
            "reason": c.reason,
            "requested_resolution": c.requested_resolution,
        }
        for c in report.test_challenges
    )
    script_error = any(
        t.failure_kind == "script" and t.status != VerificationStatus.PASSED
        for t in report.test_results
    )
    conflict = bool(report.test_challenges) or any(
        t.failure_kind == "conflict" for t in report.test_results
    )
    environment = any(
        t.failure_kind == "environment" and t.status != VerificationStatus.PASSED
        for t in report.test_results
    )
    diagnostic = EngineeringDiagnostic(
        category="contract" if conflict else "environment" if environment else "acceptance",
        disposition="needs_decision"
        if conflict or script_error
        else "restore_environment"
        if environment
        else "repair",
        code="INDEPENDENT_ACCEPTANCE_FAILED",
        summary=report.summary,
        issues=issues,
        verification="保留冻结测试与验收标准；源码修复后由独立 Test Engineer 对新源码重新验收。",
        source_hash=report.source_hash,
    )
    diagnostic.fingerprint = _digest([report.source_hash, report.suite_hash, issues])
    return diagnostic
