import json
import re
from typing import Any

from pydantic import ValidationError

from app.agents.prompts.product_manager import APP_SPEC_SYSTEM_PROMPT
from app.core.exceptions import BusinessException
from app.core.llm import chat_completion
from app.schemas.app_spec import AppSpec
from app.schemas.product_manager import ProductManagerInput

MAX_APP_SPEC_RESPONSE_CHARS = 100_000


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 字段重复")
        result[key] = value
    return result


def _reject_non_json_number(value: str) -> None:
    raise ValueError(f"不是有效 JSON 数值：{value}")


def _parse_app_spec(raw: str) -> AppSpec:
    try:
        if not isinstance(raw, str) or len(raw) > MAX_APP_SPEC_RESPONSE_CHARS:
            raise ValueError("模型正文类型或长度不符合要求")
        content = raw.strip()
        # 只兼容完整的 JSON 围栏；不从混杂文字或多个对象中猜测正文。
        fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", content, flags=re.DOTALL)
        if fence:
            content = fence.group(1)
        payload = json.loads(
            content,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_non_json_number,
        )
        return AppSpec.model_validate(payload)
    except (ValueError, TypeError, RecursionError) as exc:
        # 不把模型原文放进对外错误或日志，以免回显用户的需求内容。
        raise BusinessException("ProductManager 返回的 app_spec 格式异常") from exc


def generate_app_spec(payload: ProductManagerInput) -> AppSpec:
    """一次模型调用，一次结构校验；不自动重试、不写数据库、不生成代码。"""

    try:
        # 调用方可能修改已经验证过的列表，调用模型前重新验证并复制。
        payload = ProductManagerInput.model_validate(payload.model_dump())
    except ValidationError as exc:
        raise BusinessException("ProductManager 需求输入不符合要求") from exc
    schema = json.dumps(AppSpec.model_json_schema(), ensure_ascii=False)
    raw = chat_completion(
        messages=[
            {"role": "system", "content": f"{APP_SPEC_SYSTEM_PROMPT}\n{schema}"},
            {"role": "user", "content": payload.model_dump_json()},
        ],
        temperature=0.0,
        max_tokens=4096,
        json_output=True,
    )
    return _parse_app_spec(raw)
