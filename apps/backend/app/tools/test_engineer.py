"""Read-only verification tools and report submission for Test Engineer."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.agents.qa_context import unresolved_login_transition_test_ids
from app.core.exceptions import BusinessException, ConflictException
from app.schemas.agent_action import (
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
    inline_model_json_schema,
)
from app.schemas.qa_suite import BrowserStep, QAScript, QASuite
from app.schemas.test_report import (
    DefectRecord,
    TestReport,
    TestReportDecision,
)
from app.services.qa_assets import (
    RUNNER_VERSION,
    suite_test_results,
    validate_manifest,
    validate_suite,
)
from app.services.qa_verification import (
    DefectResolution,
    QAVerificationState,
    build_test_report_from_decision,
    is_reusable_check,
    unresolved_script_blockers,
    validate_defect_resolution,
    validate_report,
)
from app.tools import checks as check_tools
from app.tools import files as file_tools
from app.tools import qa_runner

_DEFECT_SCHEMA = inline_model_json_schema(DefectRecord)
_TEST_REPORT_DECISION_SCHEMA = inline_model_json_schema(TestReportDecision)
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
        name="adapt_test_script",
        description=(
            "只改当前独立脚本的一个定位器/路径，或在指定浏览器步骤后插入纯同步 wait_for_visible；"
            "平台保留原输入、操作、断言并生成新 suite_hash。step_index 从 0 开始。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "test_id": {"type": "string"},
                "expected_suite_hash": {"type": "string"},
                "step_index": {"type": "integer", "minimum": 0},
                "selector": {"type": "string", "minLength": 1, "maxLength": 2000},
                "path": {"type": "string", "minLength": 1, "maxLength": 2000},
                "insert_wait_for_visible_selector": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                },
            },
            "required": ["test_id", "expected_suite_hash", "step_index"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="run_acceptance_tests",
        description="冻结源码/测试快照，在无外网隔离应用执行独立 API/浏览器断言并记录证据。",
        parameters={
            "type": "object",
            "properties": {
                "override_reason": {
                    "type": "string",
                    "enum": ["final_blocked_evidence"],
                    "description": "仅测试资产修订预算耗尽后，显式允许最后一次阻断证据执行。",
                }
            },
            "additionalProperties": False,
        },
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
        name="resolve_defect",
        description=(
            "将历史业务缺陷标为已解决；须引用当前 suite_hash 和最新执行 id，"
            "且该缺陷关联的全部用例已由当前独立执行器真实通过。历史记录保留。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "defect_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "suite_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "execution_id": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "required": ["defect_id", "suite_hash", "execution_id"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="write_test_report",
        description=(
            "提交当前准确 suite_hash、execution_id、质量结论和摘要。"
            "平台从冻结 QA 状态填入完整套件、逐项执行证据、需求/检查结果与已记录缺陷；"
            "不要回传原始响应、令牌或完整测试资产。"
        ),
        parameters={
            "type": "object",
            "properties": {"report_decision": _TEST_REPORT_DECISION_SCHEMA},
            "required": ["report_decision"],
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
        if call.name == "adapt_test_script":
            if not state.suite_versions or len(state.suite_versions) >= 32:
                raise BusinessException("请先设计场景，或测试资产修订预算已用尽")
            previous = state.suite_versions[-1]
            if call.arguments.get("expected_suite_hash") != previous.digest():
                raise BusinessException("测试资产已更新；须提供准确 expected_suite_hash")
            key = str(call.arguments.get("test_id") or "")
            existing_script = previous.scripts.get(key)
            if existing_script is None:
                raise BusinessException("指定用例没有独立脚本")
            index = call.arguments.get("step_index")
            if type(index) is not int or not 0 <= index < len(existing_script.steps):
                raise BusinessException("脚本步骤索引无效")
            edits = {
                name: call.arguments[name]
                for name in ("selector", "path", "insert_wait_for_visible_selector")
                if name in call.arguments
            }
            if len(edits) != 1:
                raise BusinessException("每次只能修改一个定位器/路径或插入一个等待步骤")
            steps = list(existing_script.steps)
            step = steps[index]
            if "insert_wait_for_visible_selector" in edits:
                if not isinstance(step, BrowserStep):
                    raise BusinessException("只能在浏览器步骤后插入等待步骤")
                steps.insert(
                    index + 1,
                    BrowserStep.model_validate(
                        {
                            "action": "wait_for_visible",
                            "selector": edits["insert_wait_for_visible_selector"],
                        }
                    ),
                )
            else:
                field, value = next(iter(edits.items()))
                if field == "selector" and (
                    not isinstance(step, BrowserStep)
                    or step.action
                    not in {
                        "fill",
                        "click",
                        "select",
                        "expect_count",
                        "expect_visible",
                        "wait_for_visible",
                    }
                ):
                    raise BusinessException("该步骤没有可修改的浏览器定位器")
                if field == "path" and not (
                    step.kind == "api"
                    or (isinstance(step, BrowserStep) and step.action == "navigate")
                ):
                    raise BusinessException("该步骤不允许修改路径")
                steps[index] = type(step).model_validate(
                    {**step.model_dump(mode="json"), field: value}
                )
            updated_script = QAScript.model_validate({"steps": steps})
            suite = previous.model_copy(
                update={
                    "revision": previous.revision + 1,
                    "scripts": {**previous.scripts, key: updated_script},
                }
            )
            suite = QASuite.model_validate(suite.model_dump())
            validate_suite(suite, state.app_spec, state.acceptance_test_plan, previous)
            state.suite_versions.append(suite)
            return _ok_result(
                call,
                summary="独立脚本已安全适配并生成新测试资产版本",
                data={
                    "suite_hash": suite.digest(),
                    "revision": suite.revision,
                    "test_id": key,
                    "step_index": index,
                },
            ), None
        if call.name == "run_acceptance_tests":
            if not state.suite_versions or len(state.executions) >= state.execution_budget:
                raise BusinessException("独立场景尚未设计，或执行预算已用尽")
            suite = state.suite_versions[-1]
            unresolved = unresolved_script_blockers(state)
            unresolved_login = unresolved_login_transition_test_ids(state)
            override_reason = call.arguments.get("override_reason")
            if override_reason is not None and override_reason != "final_blocked_evidence":
                raise BusinessException("独立验收重跑覆盖理由无效")
            if override_reason is not None and (
                not (unresolved or unresolved_login) or len(state.suite_versions) < 32
            ):
                raise BusinessException("只有测试资产修订预算耗尽且仍有脚本阻断时可覆盖重跑门禁")
            if (unresolved or unresolved_login) and override_reason is None:
                raise BusinessException(
                    "上次独立执行后仍有未适配的用例："
                    + ", ".join([*unresolved, *unresolved_login])
                    + "；脚本阻断按失败步骤修订，登录点击后立即导航且首个断言超时的用例"
                    "可在点击后插入 wait_for_visible。全部处理后再运行整套独立验收"
                )
            if any(
                item.get("suite_hash") == suite.digest()
                and item.get("source_hash") == state.code_source_hash
                and item.get("runner_version") == RUNNER_VERSION
                and not (
                    any(
                        row.get("status") == "blocked" and row.get("failure_kind") == "environment"
                        for row in item.get("results", [])
                    )
                    and all(
                        row.get("status") == "passed"
                        or (
                            row.get("status") == "blocked"
                            and row.get("failure_kind") == "environment"
                        )
                        for row in item.get("results", [])
                    )
                )
                for item in state.executions
            ):
                raise BusinessException(
                    "当前源码、测试资产和执行器已有独立执行证据；"
                    "请先修订脚本生成新 suite_hash，不能重复启动同一快照"
                )
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
            if override_reason is not None:
                result.data["qa_override_reason"] = override_reason
                result.data["unresolved_script_test_ids"] = unresolved
                result.data["unresolved_login_transition_test_ids"] = unresolved_login
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
        if call.name == "resolve_defect":
            defect_id = call.arguments.get("defect_id")
            if not isinstance(defect_id, str) or defect_id not in state.defects:
                raise BusinessException("缺陷编号不存在，不能解决")
            if defect_id in state.defect_resolutions:
                raise BusinessException("缺陷已经解决")
            if not state.suite_versions or not state.executions:
                raise BusinessException("缺陷解决须有当前套件的真实独立复测")
            suite = state.suite_versions[-1]
            execution = state.executions[-1]
            if (
                call.arguments.get("suite_hash") != suite.digest()
                or call.arguments.get("execution_id") != execution.get("execution_id")
                or execution.get("runner_version") != RUNNER_VERSION
            ):
                raise BusinessException("缺陷解决须引用当前套件和最新独立执行器复测")
            validate_manifest(
                execution,
                {
                    "execution_id": execution["execution_id"],
                    "code_item_id": state.code_item_id,
                    "source_hash": state.code_source_hash,
                    "suite_hash": suite.digest(),
                },
                suite,
            )
            resolution = DefectResolution.model_validate(
                {
                    "defect_id": defect_id,
                    "suite_hash": suite.digest(),
                    "execution_id": execution["execution_id"],
                    "runner_version": RUNNER_VERSION,
                }
            )
            validate_defect_resolution(state, resolution)
            state.defect_resolutions[defect_id] = resolution
            return (
                _ok_result(
                    call,
                    summary=f"缺陷 {defect_id} 已由独立复测解决；历史记录保留",
                    data={"defect_resolution": resolution.model_dump(mode="json")},
                ),
                None,
            )
        if call.name == "write_test_report":
            if "report_decision" in call.arguments:
                report = build_test_report_from_decision(
                    TestReportDecision.model_validate(call.arguments["report_decision"]), state
                )
            else:
                # Historical direct callers may still submit the full report.
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
