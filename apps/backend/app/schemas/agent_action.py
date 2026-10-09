"""Structured LLM tool-calling results. Content may be empty when only tools are used."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def inline_model_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return a tool-provider-safe schema without local ``$ref`` pointers.

    Pydantic places ``$defs`` on the model schema. Tool definitions then embed
    that model below a named argument, where ``#/$defs/...`` incorrectly points
    at the tool root. Some providers reject the entire request before inference.
    ForgeAI's tool argument models are acyclic, so inlining is both smaller in
    failure surface and portable across OpenAI-compatible providers.
    """

    schema = deepcopy(model.model_json_schema())
    raw_definitions = schema.get("$defs")
    definitions = raw_definitions if isinstance(raw_definitions, dict) else {}

    def expand(value: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(value, list):
            return [expand(item, stack) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/").replace("~1", "/").replace("~0", "~")
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ValueError(f"JSON Schema 引用了不存在的定义：{reference}")
            if name in stack:
                raise ValueError(f"工具参数 JSON Schema 包含递归定义：{name}")
            expanded = expand(target, (*stack, name))
            siblings = {
                key: expand(item, stack)
                for key, item in value.items()
                if key not in {"$ref", "$defs"}
            }
            return {**expanded, **siblings}
        return {key: expand(item, stack) for key, item in value.items() if key != "$defs"}

    result = expand(schema)
    if not isinstance(result, dict):
        raise ValueError("模型 JSON Schema 根节点必须是对象")
    return result


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=2000)
    parameters: dict[str, Any]


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ChatWithToolsResult(BaseModel):
    """Native or JSON-protocol model turn. Empty content is valid when tool_calls exist."""

    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    model: str | None = None
    usage: TokenUsage | None = None
    protocol: Literal["native", "json"] = "native"


class ToolExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    name: str
    ok: bool
    error_code: str | None = None
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    arguments: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
