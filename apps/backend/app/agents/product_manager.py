from __future__ import annotations

import json

from pydantic import ValidationError

from app.agents.prompts.prd_system import PRD_SYSTEM_PROMPT
from app.agents.website_spec import WebsiteSpecification
from app.core.exceptions import BusinessException
from app.core.llm import chat_completion


class ProductManagerAgent:
    def run(self, user_requirement: str, *, product_name: str | None = None) -> str:
        requirement = (user_requirement or "").strip()
        if not requirement:
            raise ValueError("用户需求不能为空")

        name = (product_name or "").strip()
        name_line = f"产品名称：{name}\n\n" if name else ""
        user_content = f"{name_line}产品需求：\n{requirement}"

        content = chat_completion(
            messages=[
                {"role": "system", "content": PRD_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
            max_tokens=4096,
            json_output=True,
        )

        if content.startswith("```"):
            lines = content.splitlines()
            content = "\n".join(lines[1:-1]).strip()

        try:
            specification = WebsiteSpecification.model_validate_json(content)
        except (ValidationError, ValueError) as exc:
            raise BusinessException("Product Manager 返回的网站规格格式无效，请重试") from exc

        return json.dumps(specification.model_dump(), ensure_ascii=False, indent=2)
