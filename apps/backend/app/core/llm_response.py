"""Provider-response normalization kept outside product agents."""

from __future__ import annotations

import re
from typing import Literal

from app.core.exceptions import BusinessException

CompletionContentKind = Literal["text", "source"]

# Compatibility tokens emitted by some OpenAI-compatible gateways. They belong
# to the transport boundary and must never become Code Writer implementation
# rules. Match protocol tag families structurally instead of adding separate
# opening/closing substrings for every observed leak.
_OUTER_TEXT_ENVELOPES = (("<final>", "</final>"),)
_SOURCE_PROTOCOL_TAG_NAMES = frozenset(
    {
        "analysis",
        "assistant",
        "final",
        "function",
        "function_call",
        "function_calls",
        "invoke",
        "parameter",
        "parameters",
        "tool",
        "tool_call",
        "tool_calls",
    }
)
_PROTOCOL_TAG = re.compile(
    r"<\s*/?\s*(?P<name>[a-zA-Z_][\w:.-]*)\b[^<>]*>",
    flags=re.IGNORECASE,
)
_NON_XML_PROTOCOL_MARKERS = ("dsml", "<|tool_call", "<|function")


class CompletionContentError(BusinessException):
    """The provider returned text that cannot satisfy the requested contract."""


def _contains_source_protocol(value: str) -> bool:
    lowered = value.lower()
    if any(marker in lowered for marker in _NON_XML_PROTOCOL_MARKERS):
        return True
    for match in _PROTOCOL_TAG.finditer(value):
        raw_name = match.group("name")
        name = raw_name.rsplit(":", maxsplit=1)[-1]
        if name != name.lower() or name not in _SOURCE_PROTOCOL_TAG_NAMES:
            continue
        line_start = value.rfind("\n", 0, match.start()) + 1
        line_end = value.find("\n", match.end())
        if line_end == -1:
            line_end = len(value)
        prefix = value[line_start : match.start()]
        suffix = value[match.end() : line_end]
        if not prefix.strip() or not suffix.strip():
            return True
    return False


def normalize_completion_content(
    value: str,
    *,
    kind: CompletionContentKind = "text",
) -> str:
    """Remove known transport envelopes and enforce the requested content kind."""

    content = value.strip()
    lowered = content.lower()
    for opening, closing in _OUTER_TEXT_ENVELOPES:
        if lowered.startswith(opening):
            content = content[len(opening) :].lstrip()
            lowered = content.lower()
        if lowered.endswith(closing):
            content = content[: -len(closing)].rstrip()
            lowered = content.lower()
    if not content:
        raise CompletionContentError("大模型最终输出为空")
    if kind == "source" and _contains_source_protocol(content):
        raise CompletionContentError("大模型源码响应包含提供方协议标记")
    return content
