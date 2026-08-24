from __future__ import annotations

import json

from pydantic import ValidationError

from app.agents.website_builder import strip_json_fence
from app.core.exceptions import BusinessException
from app.core.llm import chat_completion
from app.schemas.project import ProjectElementAiEdit, WebsiteElementChanges, WebsiteElementPatch

ELEMENT_EDITOR_SYSTEM_PROMPT = """
你是网站可视化编辑器中的元素设计助手。你只修改用户当前选中的一个元素。

必须只返回一个 JSON 对象，不要 Markdown、代码围栏或解释：
{
  "text": "可选的新文字；不修改时为 null",
  "styles": {"允许的 CSS 属性": "合法值"}
}

允许的样式属性只有：color、background-color、border-color、font-size、font-weight、
font-family、text-align、margin-top、margin-right、margin-bottom、margin-left、padding-top、
padding-right、padding-bottom、padding-left、gap、border-radius。
颜色只能用 #RRGGBB；尺寸必须带 px、rem、em 或 %；font-weight 只能为 100 到 900；
font-family 只能为 Arial、Georgia、Inter、system-ui、sans-serif、serif；
text-align 只能为 left、center、right、justify。
只返回实现用户指令所必需的字段，不要重置其他样式。若 text_editable=false，text 必须为 null。
""".strip()


def suggest_element_patch(payload: ProjectElementAiEdit) -> WebsiteElementPatch:
    content = chat_completion(
        messages=[
            {"role": "system", "content": ELEMENT_EDITOR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "element": {
                            "tag_name": payload.tag_name,
                            "text": payload.text,
                            "text_editable": payload.text_editable,
                            "current_styles": payload.styles,
                        },
                        "instruction": payload.instruction,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
        temperature=0.1,
        max_tokens=1000,
        json_output=True,
    )
    try:
        changes = WebsiteElementChanges.model_validate_json(strip_json_fence(content))
    except (ValidationError, ValueError) as exc:
        raise BusinessException("元素设计助手返回的修改格式无效") from exc
    if changes.text is None and not changes.styles:
        raise BusinessException("元素设计助手没有生成可应用的修改")
    if not payload.text_editable and changes.text is not None:
        raise BusinessException("当前元素包含子元素，AI 不能直接替换其文字")
    return WebsiteElementPatch(element_id=payload.element_id, changes=changes)
