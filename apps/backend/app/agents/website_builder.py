from __future__ import annotations

import json

from pydantic import BaseModel, Field, ValidationError

from app.agents.prompts.website_builder_system import WEBSITE_BUILDER_SYSTEM_PROMPT
from app.agents.website_spec import WebsiteSpecification
from app.core.exceptions import BusinessException
from app.core.llm import chat_completion


class GeneratedWebsiteFiles(BaseModel):
    index_html: str = Field(alias="index.html", min_length=100, max_length=200_000)
    style_css: str = Field(alias="style.css", min_length=20, max_length=200_000)
    script_js: str = Field(alias="script.js", max_length=100_000)


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

        if content.startswith("```"):
            lines = content.splitlines()
            content = "\n".join(lines[1:-1]).strip()

        try:
            files = GeneratedWebsiteFiles.model_validate_json(content)
        except (ValidationError, ValueError) as exc:
            raise BusinessException("Website Builder 返回的文件格式无效，请重试") from exc

        html = files.index_html.lower()
        if "<!doctype html" not in html or "<body" not in html:
            raise BusinessException("Website Builder 返回的 HTML 文档不完整，请重试")

        return json.dumps(files.model_dump(by_alias=True), ensure_ascii=False, indent=2)
