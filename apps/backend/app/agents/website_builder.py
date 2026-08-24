from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field, ValidationError

from app.agents.prompts.website_builder_system import WEBSITE_BUILDER_SYSTEM_PROMPT
from app.agents.website_spec import WebsiteSpecification
from app.core.exceptions import BusinessException
from app.core.llm import chat_completion

logger = logging.getLogger("forgeai.agent.website_builder")


class GeneratedWebsiteFiles(BaseModel):
    index_html: str = Field(alias="index.html", min_length=100, max_length=200_000)
    style_css: str = Field(alias="style.css", min_length=20, max_length=200_000)
    script_js: str = Field(alias="script.js", max_length=100_000)


def strip_json_fence(content: str) -> str:
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _parse_files(content: str, *, stage: str) -> GeneratedWebsiteFiles:
    normalized = strip_json_fence(content)
    try:
        return GeneratedWebsiteFiles.model_validate_json(normalized)
    except (ValidationError, ValueError) as exc:
        diagnostics = {
            "stage": stage,
            "content_length": len(normalized),
            "content_preview": repr(normalized[:240]),
            "content_tail": repr(normalized[-240:]),
            "validation_error": str(exc)[:1000],
        }
        logger.warning("Website Builder output validation failed: %s", diagnostics)
        message = (
            "Website Builder 修复后的文件格式无效"
            if stage.startswith("repair")
            else "Website Builder 返回的文件格式无效，请重试"
        )
        raise BusinessException(message, data=diagnostics) from exc


class WebsiteBuilderAgent:
    def run(self, approved_spec: str) -> str:
        try:
            specification = WebsiteSpecification.model_validate_json(approved_spec)
        except (ValidationError, ValueError) as exc:
            raise BusinessException("已批准的网站规格格式无效") from exc

        content = chat_completion(
            messages=[
                {"role": "system", "content": WEBSITE_BUILDER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "请根据以下已批准的网站规格生成网站文件。\n\n"
                        f"{specification.model_dump_json(indent=2)}"
                    ),
                },
            ],
            temperature=0.2,
            max_tokens=8192,
            json_output=True,
        )

        files = _parse_files(content, stage="build")

        html = files.index_html.lower()
        if "<!doctype html" not in html or "<body" not in html:
            raise BusinessException("Website Builder 返回的 HTML 文档不完整，请重试")

        return json.dumps(files.model_dump(by_alias=True), ensure_ascii=False, indent=2)

    def repair(self, approved_spec: str, current_files: str, issues: str) -> str:
        try:
            specification = WebsiteSpecification.model_validate_json(approved_spec)
            GeneratedWebsiteFiles.model_validate_json(current_files)
        except (ValidationError, ValueError) as exc:
            raise BusinessException("待修复的网站数据格式无效") from exc

        content = chat_completion(
            messages=[
                {"role": "system", "content": WEBSITE_BUILDER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "请修复下面网站文件中的质量问题。保持未被指出的正确实现，"
                        "不要扩大产品范围。修复后仍只输出三个网站文件的 JSON。\n\n"
                        f"已批准规格：\n{specification.model_dump_json(indent=2)}\n\n"
                        f"当前文件：\n{current_files}\n\n"
                        f"质量问题：\n{issues}"
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=16384,
            json_output=True,
        )
        try:
            files = _parse_files(content, stage="repair")
        except BusinessException as first_error:
            logger.info("Retrying Website Builder output as a format-only recovery")
            recovered_content = chat_completion(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是 JSON 文件整理器。只输出合法 JSON 对象，且必须仅包含 "
                            '"index.html"、"style.css"、"script.js" 三个字符串字段。'
                            "不要解释，不要使用 Markdown，不要修改已有网站实现。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "请将下面的模型输出恢复为完整合法的三个文件 JSON。"
                            "若结尾被截断，请补齐必要的闭合内容：\n\n"
                            f"{content}"
                        ),
                    },
                ],
                temperature=0.0,
                max_tokens=16384,
                json_output=True,
            )
            try:
                files = _parse_files(recovered_content, stage="repair_recovery")
            except BusinessException as recovery_error:
                recovery_error.data = {
                    "initial_error": first_error.data,
                    "recovery_error": recovery_error.data,
                }
                raise recovery_error from first_error
        return json.dumps(files.model_dump(by_alias=True), ensure_ascii=False, indent=2)
