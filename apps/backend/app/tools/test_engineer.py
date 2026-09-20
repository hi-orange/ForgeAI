"""Read-only verification tools and report submission for Test Engineer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.exceptions import BusinessException, ConflictException
from app.schemas.agent_action import ToolCall, ToolDefinition, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.schemas.test_report import (
    DefectRecord,
    TestReport,
    VerificationStatus,
)
from app.tools import checks as check_tools
from app.tools import files as file_tools

_DEFECT_SCHEMA = DefectRecord.model_json_schema()
_TEST_REPORT_SCHEMA = TestReport.model_json_schema()

TEST_ENGINEER_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="read_artifact",
        description="读取冻结的 app_spec、system_design 或准确代码身份。",
        parameters={
            "type": "object",
            "properties": {
                "artifact": {
                    "type": "string",
                    "enum": ["app_spec", "system_design", "code"],
                }
            },
            "required": ["artifact"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="list_files",
        description="列出准确代码工作区内的文本文件。",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "maxLength": 500}},
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="read_file",
        description="只读查看代码文件和 content_hash，不允许修改。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 500},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="search_code",
        description="在准确代码工作区中搜索与需求、接口、权限或缺陷相关的实现。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 300},
                "path_filter": {"type": "string", "maxLength": 500},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="run_check",
        description="在隔离、离线环境运行数据库、后端、前端或完整检查。",
        parameters={
            "type": "object",
            "properties": {
                "check_id": {
                    "type": "string",
                    "enum": ["database", "backend", "frontend", "all"],
                }
            },
            "required": ["check_id"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="record_defect",
        description="记录一个有证据、严重度和需求关联的缺陷；不修改代码。",
        parameters={
            "type": "object",
            "properties": {"defect": _DEFECT_SCHEMA},
            "required": ["defect"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="write_test_report",
        description="提交覆盖 PRD、检查结果、缺陷和质量结论的最终 test_report。",
        parameters={
            "type": "object",
            "properties": {"test_report": _TEST_REPORT_SCHEMA},
            "required": ["test_report"],
            "additionalProperties": False,
        },
    ),
]


@dataclass(slots=True)
class TestEngineerToolState:
    app_spec: AppSpec
    code_item_id: str
    code_source_hash: str
    workspace_root: Path
    system_design: SystemDesign | None = None
    checks: dict[str, ToolExecutionResult] = field(default_factory=dict)
    defects: dict[str, DefectRecord] = field(default_factory=dict)


def _result_status(result: ToolExecutionResult) -> VerificationStatus:
    if result.ok:
        return VerificationStatus.PASSED
    if result.error_code in {
        "CHECK_ENVIRONMENT_UNAVAILABLE",
        "CHECK_TIMEOUT",
        "SOURCE_CHANGED",
        "SOURCE_MISMATCH",
    }:
        return VerificationStatus.BLOCKED
    return VerificationStatus.FAILED


def _validated_report(call: ToolCall, state: TestEngineerToolState) -> TestReport:
    try:
        report = TestReport.model_validate(call.arguments.get("test_report"))
    except ValidationError as exc:
        raise BusinessException("测试报告缺少需求覆盖、检查证据、缺陷或质量结论") from exc
    if (report.code_item_id, report.source_hash) != (
        state.code_item_id,
        state.code_source_hash,
    ):
        raise BusinessException("测试报告引用的不是本轮准确代码结果")
    acceptance_ids = {item.id for item in state.app_spec.acceptance_criteria}
    reported_ids = {item.requirement_id for item in report.requirement_results}
    if reported_ids != acceptance_ids:
        raise BusinessException("测试报告必须逐条覆盖 PRD 验收条件")
    if "all" not in state.checks:
        raise BusinessException("提交测试报告前必须运行完整 all 检查")
    reported_checks = {item.check_id: item for item in report.check_results}
    if set(reported_checks) != set(state.checks):
        raise BusinessException("测试报告必须记录本轮全部实际检查")
    for check_id, result in state.checks.items():
        if reported_checks[check_id].status != _result_status(result):
            raise BusinessException(f"检查 {check_id} 的报告状态与实际结果不一致")
    reported_defects = {item.defect_id: item for item in report.defects}
    if set(reported_defects) != set(state.defects) or any(
        reported_defects[key] != value for key, value in state.defects.items()
    ):
        raise BusinessException("测试报告中的缺陷必须来自本轮独立记录")
    if (
        any(item.status == VerificationStatus.FAILED for item in report.requirement_results)
        and not report.defects
    ):
        raise BusinessException("验收失败时必须记录至少一个缺陷")
    return report


def _ok_result(
    call: ToolCall,
    *,
    summary: str,
    data: dict[str, Any] | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_call_id=call.id,
        name=call.name,
        ok=True,
        summary=summary,
        data=data or {},
        arguments=dict(call.arguments),
    )


def execute_test_engineer_tool(
    call: ToolCall,
    state: TestEngineerToolState,
) -> tuple[ToolExecutionResult, TestReport | None]:
    try:
        if call.name == "read_artifact":
            artifact = str(call.arguments.get("artifact") or "")
            if artifact == "app_spec":
                data: dict[str, Any] = {"app_spec": state.app_spec.model_dump(mode="json")}
            elif artifact == "system_design":
                if state.system_design is None:
                    raise BusinessException("本轮没有 system_design 输入")
                data = {"system_design": state.system_design.model_dump(mode="json")}
            elif artifact == "code":
                data = {
                    "code_item_id": state.code_item_id,
                    "source_hash": state.code_source_hash,
                }
            else:
                raise BusinessException("未知成果类型")
            return _ok_result(call, summary=f"已读取冻结成果：{artifact}", data=data), None
        if call.name == "list_files":
            result = file_tools.list_files(
                state.workspace_root,
                path=str(call.arguments.get("path") or "."),
                tool_call_id=call.id,
            )
            return result.model_copy(
                update={"name": call.name, "arguments": dict(call.arguments)}
            ), None
        if call.name == "read_file":
            result = file_tools.read_file(
                state.workspace_root,
                path=str(call.arguments.get("path") or ""),
                start_line=int(call.arguments.get("start_line") or 1),
                end_line=(
                    int(call.arguments["end_line"])
                    if call.arguments.get("end_line") is not None
                    else None
                ),
                tool_call_id=call.id,
            )
            return result.model_copy(update={"arguments": dict(call.arguments)}), None
        if call.name == "search_code":
            result = file_tools.search_code(
                state.workspace_root,
                query=str(call.arguments.get("query") or ""),
                path_filter=(
                    str(call.arguments["path_filter"])
                    if call.arguments.get("path_filter")
                    else None
                ),
                tool_call_id=call.id,
            )
            return result.model_copy(update={"arguments": dict(call.arguments)}), None
        if call.name == "run_check":
            check_id = str(call.arguments.get("check_id") or "")
            result = check_tools.run_check(
                state.workspace_root, check_id=check_id, tool_call_id=call.id
            ).model_copy(update={"arguments": dict(call.arguments)})
            observed_hash = result.data.get("source_hash")
            if isinstance(observed_hash, str) and observed_hash != state.code_source_hash:
                result = result.model_copy(
                    update={
                        "ok": False,
                        "error_code": "SOURCE_MISMATCH",
                        "summary": "检查源码与指定代码结果不一致",
                    }
                )
            state.checks[check_id] = result
            return result, None
        if call.name == "record_defect":
            try:
                defect = DefectRecord.model_validate(call.arguments.get("defect"))
            except ValidationError as exc:
                raise BusinessException("缺陷记录格式不完整") from exc
            if defect.defect_id in state.defects:
                raise BusinessException("缺陷编号已经记录")
            requirement_ids = {item.id for item in state.app_spec.acceptance_criteria}
            if not set(defect.related_requirement_ids).issubset(requirement_ids):
                raise BusinessException("缺陷引用了不存在的 PRD 验收条件")
            state.defects[defect.defect_id] = defect
            return (
                _ok_result(
                    call,
                    summary=f"已记录缺陷：{defect.defect_id}",
                    data={"defect": defect.model_dump(mode="json")},
                ),
                None,
            )
        if call.name == "write_test_report":
            report = _validated_report(call, state)
            return (
                _ok_result(
                    call,
                    summary=f"已提交质量结论：{report.quality_conclusion.value}",
                    data={
                        "quality_conclusion": report.quality_conclusion.value,
                        "defect_count": len(report.defects),
                    },
                ),
                report,
            )
        raise BusinessException(f"Test Engineer 无权使用工具：{call.name}")
    except (BusinessException, ConflictException, OSError, ValueError) as exc:
        return (
            ToolExecutionResult(
                tool_call_id=call.id,
                name=call.name,
                ok=False,
                error_code="TOOL_REJECTED",
                summary=str(exc),
                arguments=dict(call.arguments),
            ),
            None,
        )
