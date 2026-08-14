from __future__ import annotations

from app.agents.prompts.prd_system import PRD_SYSTEM_PROMPT
from app.core.llm import chat_completion


class ProductManagerAgent:
    def run(self, user_requirement: str, *, product_name: str | None = None) -> str:
        requirement = (user_requirement or "").strip()
        if not requirement:
            raise ValueError("用户需求不能为空")

        name = (product_name or "").strip()
        name_line = f"产品名称：{name}\n\n" if name else ""
        user_content = f"{name_line}产品需求：\n{requirement}"

        return chat_completion(
            messages=[
                {"role": "system", "content": PRD_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
            max_tokens=8192,
        )
