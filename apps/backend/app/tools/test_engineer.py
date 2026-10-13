"""Read-only verification tools and report submission for Test Engineer."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.core.exceptions import BusinessException, ConflictException
from app.schemas.agent_action import (
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
    inline_model_json_schema,
)
from app.schemas.qa_suite import QAScript, QASuite
from app.schemas.test_report import (
    DefectRecord,
    TestReport,
)
from app.services.qa_assets import suite_test_results, validate_suite
from app.services.qa_verification import QAVerificationState, is_reusable_check, validate_report
from app.tools import checks as check_tools
from app.tools import files as file_tools
from app.tools import qa_runner

_DEFECT_SCHEMA = inline_model_json_schema(DefectRecord)
_TEST_REPORT_SCHEMA = inline_model_json_schema(TestReport)
TEST_ENGINEER_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="design_test_cases",
        description="提交需求驱动场景草稿；平台校验获批预期和覆盖后冻结版本，不写业务源码。",
        parameters={
            "type": "object",
            "properties": {"suite": inline_model_json_schema(QASuite)},
            "required": ["suite"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="write_test_script",
        description="为独立场景生成受限 API/浏览器脚本；适配修订必须保留全部操作和断言。",
        parameters={
            "type": "object",
            "properties": {
                "test_id": {"type": "string"},
                "expected_suite_hash": {"type": "string"},
                "script": inline_model_json_schema(QAScript),
            },
            "required": ["test_id", "expected_suite_hash", "script"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="run_acceptance_tests",
        description="冻结源码/测试快照，在无外网隔离应用执行独立 API/浏览器断言并记录证据。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolDefinition(
        name="read_artifact",
        description="读取冻结的 app_spec、system_design 或准确代码身份。",
        parameters={
            "type": "object",
            "properties": {
                "artifact": {
                    "type": "string",
                    "enum": ["app_spec", "system_design", "acceptance_test_plan", "code"],
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
        description="在隔离环境运行数据库、后端、前端或完整检查（可按生成清单安装依赖）。",
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
        name="capture_screenshots",
        description=(
            "在隔离环境启动准确代码，用 Chromium 生成 1440x900 桌面端和 390x844 移动端截图，"
            "并检查控制台错误、页面异常和横向溢出。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "route": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": "站内绝对路径，例如 / 或 /jobs。",
                }
            },
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
    state: QAVerificationState,
) -> tuple[ToolExecutionResult, TestReport | None]:
    try:
        if call.name == "design_test_cases":
            suite = QASuite.model_validate(call.arguments.get("suite"))
            previous = state.suite_versions[-1] if state.suite_versions else None
            validate_suite(suite, state.app_spec, state.acceptance_test_plan, previous)
            if len(state.suite_versions) >= 32:
                raise BusinessException("独立测试资产修订预算已用尽")
            suite = suite.model_copy(update={"revision": previous.revision + 1 if previous else 1})
            state.suite_versions.append(suite)
            return _ok_result(
                call,
                summary="测试场景已校验并冻结",
                data={
                    "suite_hash": suite.digest(),
                    "suite": suite.model_dump(mode="json"),
                },
            ), None
        if call.name == "write_test_script":
            if not state.suite_versions or len(state.suite_versions) >= 32:
                raise BusinessException("请先设计场景，或测试资产修订预算已用尽")
            previous = state.suite_versions[-1]
            if call.arguments.get("expected_suite_hash") != previous.digest():
                raise BusinessException("测试资产已更新；须提供准确 expected_suite_hash")
            script = QAScript.model_validate(call.arguments.get("script"))
            key = str(call.arguments.get("test_id") or "")
            suite = previous.model_copy(
                update={
                    "revision": previous.revision + 1,
                    "scripts": {**previous.scripts, key: script},
                }
            )
            suite = QASuite.model_validate(suite.model_dump())
            validate_suite(suite, state.app_spec, state.acceptance_test_plan, previous)
            state.suite_versions.append(suite)
            return _ok_result(
                call,
                summary="独立脚本已生成新版本",
                data={
                    "suite_hash": suite.digest(),
                    "suite": suite.model_dump(mode="json"),
                },
            ), None
        if call.name == "run_acceptance_tests":
            if not state.suite_versions or len(state.executions) >= 6:
                raise BusinessException("独立场景尚未设计，或执行预算已用尽")
            suite = state.suite_versions[-1]
            result = qa_runner.run_acceptance_tests(
                state.workspace_root,
                code_item_id=state.code_item_id,
                source_hash=state.code_source_hash,
                suite=suite,
                tool_call_id=call.id,
            )
            manifest = result.data.get("manifest")
            if isinstance(manifest, dict):
                state.executions.append(manifest)
            state.checks["acceptance"] = result
            return result.model_copy(
                update={
                    "data": {
                        **result.data,
                        "suite": suite.model_dump(mode="json"),
                        "suite_hash": suite.digest(),
                        "test_results": suite_test_results(
                            suite, manifest if isinstance(manifest, dict) else {}
                        ),
                    }
                }
            ), None
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
            elif artifact == "acceptance_test_plan":
                if state.acceptance_test_plan is None:
                    raise BusinessException("本轮没有冻结 acceptance_test_plan 输入")
                data = {
                    "test_plan_item_id": state.acceptance_test_plan_item_id,
                    "test_hash": state.acceptance_test_hash,
                    "acceptance_test_plan": state.acceptance_test_plan.model_dump(mode="json"),
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
            cached = state.checks.get(check_id)
            if cached is not None and is_reusable_check(cached):
                reused = cached.model_copy(
                    update={
                        "tool_call_id": call.id,
                        "summary": f"复用已完成检查：{cached.summary}",
                        "arguments": dict(call.arguments),
                    }
                )
                state.checks[check_id] = reused
                return reused, None
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
        if call.name == "capture_screenshots":
            if "all" not in state.checks:
                raise BusinessException('截图前必须先 run_check(check_id="all")')
            route = str(call.arguments.get("route") or "/")
            cached = next(
                (
                    result
                    for check_id, result in state.checks.items()
                    if check_id.startswith("visual")
                    and result.data.get("route") == route
                    and is_reusable_check(result)
                ),
                None,
            )
            if cached is not None:
                reused = cached.model_copy(
                    update={
                        "tool_call_id": call.id,
                        "summary": f"复用已完成截图：{cached.summary}",
                        "arguments": dict(call.arguments),
                    }
                )
                check_id = str(reused.data.get("check_id") or "visual")
                state.checks[check_id] = reused
                return reused, None
            result = check_tools.capture_screenshots(
                state.workspace_root,
                route=route,
                tool_call_id=call.id,
            ).model_copy(update={"arguments": dict(call.arguments)})
            observed_hash = result.data.get("source_hash")
            if isinstance(observed_hash, str) and observed_hash != state.code_source_hash:
                result = result.model_copy(
                    update={
                        "ok": False,
                        "error_code": "SOURCE_MISMATCH",
                        "summary": "截图源码与指定代码结果不一致",
                    }
                )
            check_id = str(result.data.get("check_id") or "visual")
            state.checks[check_id] = result
            return result, None
        if call.name == "record_defect":
            if not state.suite_versions:
                raise BusinessException("业务缺陷须先有独立用例的实际失败证据")
            try:
                defect = DefectRecord.model_validate(call.arguments.get("defect"))
            except ValidationError as exc:
                raise BusinessException("缺陷记录格式不完整") from exc
            if defect.defect_id in state.defects:
                raise BusinessException("缺陷编号已经记录")
            requirement_ids = {item.id for item in state.app_spec.acceptance_criteria}
            if not set(defect.related_requirement_ids).issubset(requirement_ids):
                raise BusinessException("缺陷引用了不存在的 PRD 验收条件")
            if state.suite_versions:
                suite = state.suite_versions[-1]
                execution = next(
                    (
                        item
                        for item in reversed(state.executions)
                        if item["suite_hash"] == suite.digest()
                    ),
                    {},
                )
                failed = {
                    row["test_id"]
                    for row in execution.get("results", [])
                    if row["status"] == "failed" and row["failure_kind"] == "product"
                }
                if (
                    not defect.related_test_ids
                    or not set(defect.related_test_ids) <= failed
                    or defect.execution_id != execution.get("execution_id")
                ):
                    raise BusinessException("业务缺陷须引用本轮实际失败用例和执行身份")
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
            report = validate_report(
                TestReport.model_validate(call.arguments.get("test_report")), state
            )
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
