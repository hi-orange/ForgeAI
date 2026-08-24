from __future__ import annotations

import json
import re
from collections import Counter
from html.parser import HTMLParser
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.agents.prompts.website_quality_system import WEBSITE_QUALITY_SYSTEM_PROMPT
from app.agents.website_builder import GeneratedWebsiteFiles, strip_json_fence
from app.agents.website_spec import WebsiteSpecification
from app.core.exceptions import BusinessException
from app.core.llm import chat_completion


class ValidationIssue(BaseModel):
    severity: Literal["error", "warning"]
    category: str = Field(min_length=1, max_length=50)
    file: Literal["index.html", "style.css", "script.js", "project"]
    description: str = Field(min_length=1, max_length=500)
    suggestion: str = Field(min_length=1, max_length=500)


class WebsiteValidationReport(BaseModel):
    passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list, max_length=30)


class _DocumentInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.forge_ids: list[str] = []
        self.remote_resources: list[str] = []
        self.form_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self.form_count += 1
        element_id = attributes.get("id")
        if element_id:
            self.ids.append(element_id)
        forge_id = attributes.get("data-forge-id")
        if forge_id:
            self.forge_ids.append(forge_id)
        for name in ("src", "href"):
            value = attributes.get(name) or ""
            if value.startswith(("http://", "https://", "//")):
                self.remote_resources.append(f"{tag}[{name}={value}]")


def deterministic_issues(files_json: str) -> list[ValidationIssue]:
    files = GeneratedWebsiteFiles.model_validate_json(files_json)
    inspector = _DocumentInspector()
    inspector.feed(files.index_html)
    issues: list[ValidationIssue] = []

    duplicates = [item for item, count in Counter(inspector.ids).items() if count > 1]
    if duplicates:
        issues.append(
            ValidationIssue(
                severity="error",
                category="consistency",
                file="index.html",
                description=f"存在重复 DOM id：{', '.join(duplicates[:10])}",
                suggestion="为每个元素使用唯一 id，并同步修正 JavaScript 选择器。",
            )
        )

    duplicate_forge_ids = [
        item for item, count in Counter(inspector.forge_ids).items() if count > 1
    ]
    if duplicate_forge_ids:
        issues.append(
            ValidationIssue(
                severity="error",
                category="consistency",
                file="index.html",
                description=f"存在重复 data-forge-id：{', '.join(duplicate_forge_ids[:10])}",
                suggestion="为每个可编辑元素保留唯一且稳定的 data-forge-id。",
            )
        )

    referenced_ids = set(re.findall(r"getElementById\([\"']([^\"']+)[\"']\)", files.script_js))
    dynamic_ids = set(
        re.findall(r"\bid\s*=\s*[\"']([^\"']+)[\"']", files.script_js)
        + re.findall(
            r"setAttribute\(\s*[\"']id[\"']\s*,\s*[\"']([^\"']+)[\"']",
            files.script_js,
        )
    )
    missing_ids = sorted(referenced_ids - set(inspector.ids) - dynamic_ids)
    if missing_ids:
        issues.append(
            ValidationIssue(
                severity="error",
                category="consistency",
                file="script.js",
                description=f"JavaScript 引用了不存在的 DOM id：{', '.join(missing_ids[:10])}",
                suggestion="修正选择器或补齐对应元素，确保脚本初始化不会中断。",
            )
        )

    if inspector.remote_resources:
        issues.append(
            ValidationIssue(
                severity="error",
                category="security",
                file="index.html",
                description="包含未经规格批准的远程资源引用。",
                suggestion="移除远程资源，改用本地 HTML、CSS 或内联 SVG 实现。",
            )
        )

    network_patterns = (r"\bfetch\s*\(", r"\bXMLHttpRequest\b", r"\bWebSocket\s*\(")
    if any(re.search(pattern, files.script_js) for pattern in network_patterns):
        issues.append(
            ValidationIssue(
                severity="error",
                category="security",
                file="script.js",
                description="JavaScript 包含网络请求能力。",
                suggestion="移除网络调用，使用本地模拟数据和本地交互。",
            )
        )

    prevents_form_submission = bool(
        re.search(r"preventDefault\s*\(", files.script_js)
        and re.search(r"[\"']submit[\"']", files.script_js)
    )
    if inspector.form_count and not prevents_form_submission:
        issues.append(
            ValidationIssue(
                severity="error",
                category="interaction",
                file="script.js",
                description="页面包含表单，但没有可靠拦截浏览器默认提交行为。",
                suggestion=(
                    "监听表单 submit 事件并调用 preventDefault()，完成校验后在页面内显示反馈。"
                ),
            )
        )

    return issues


class WebsiteQualityAgent:
    def run(self, approved_spec: str, generated_files: str) -> WebsiteValidationReport:
        try:
            specification = WebsiteSpecification.model_validate_json(approved_spec)
            files = GeneratedWebsiteFiles.model_validate_json(generated_files)
        except (ValidationError, ValueError) as exc:
            raise BusinessException("网站质量检查输入格式无效") from exc

        content = chat_completion(
            messages=[
                {"role": "system", "content": WEBSITE_QUALITY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "请审查以下网站。\n\n"
                        f"已批准规格：\n{specification.model_dump_json(indent=2)}\n\n"
                        "生成文件：\n"
                        f"{json.dumps(files.model_dump(by_alias=True), ensure_ascii=False)}"
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=4096,
            json_output=True,
        )
        try:
            report = WebsiteValidationReport.model_validate_json(strip_json_fence(content))
        except (ValidationError, ValueError) as exc:
            raise BusinessException("Website QA 返回的验证报告格式无效") from exc

        has_errors = any(issue.severity == "error" for issue in report.issues)
        return report.model_copy(update={"passed": not has_errors})
