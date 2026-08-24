from __future__ import annotations

import html
import json
import re
from collections import Counter

from app.agents.website_builder import GeneratedWebsiteFiles
from app.core.exceptions import BusinessException
from app.schemas.project import WebsiteElementPatch

EDITABLE_TAG_PATTERN = re.compile(
    r"<(?P<tag>h[1-6]|p|a|button|label|span|section|article|div)\b(?P<attrs>[^<>]*?)>",
    re.IGNORECASE,
)
FORGE_ID_PATTERN = re.compile(r"\bdata-forge-id\s*=\s*([\"'])(?P<id>[^\"']+)\1", re.IGNORECASE)
OVERRIDE_PATTERN = re.compile(
    r"/\* forge-editor-overrides:start \*/.*?/\* forge-editor-overrides:end \*/",
    re.DOTALL,
)
OVERRIDE_RULE_PATTERN = re.compile(
    r'\[data-forge-id="(?P<id>[a-zA-Z0-9_-]+)"\]\s*\{(?P<body>.*?)\}',
    re.DOTALL,
)
STYLE_DECLARATION_PATTERN = re.compile(
    r"(?P<name>color|background-color|border-color|font-size|font-weight|font-family|"
    r"text-align|margin-top|margin-right|margin-bottom|margin-left|padding-top|"
    r"padding-right|padding-bottom|padding-left|gap|border-radius)\s*:\s*"
    r"(?P<value>[^;{}]+)\s*;"
)


def ensure_editable_ids(files_json: str) -> str:
    files = GeneratedWebsiteFiles.model_validate_json(files_json)
    existing_ids = FORGE_ID_PATTERN.findall(files.index_html)
    used = {match[1] for match in existing_ids}
    counters: Counter[str] = Counter()

    def annotate(match: re.Match[str]) -> str:
        if FORGE_ID_PATTERN.search(match.group(0)):
            return match.group(0)
        tag = match.group("tag").lower()
        counters[tag] += 1
        candidate = f"forge-{tag}-{counters[tag]}"
        while candidate in used:
            counters[tag] += 1
            candidate = f"forge-{tag}-{counters[tag]}"
        used.add(candidate)
        return f'<{match.group("tag")}{match.group("attrs")} data-forge-id="{candidate}">'

    annotated_html = EDITABLE_TAG_PATTERN.sub(annotate, files.index_html)
    updated = files.model_copy(update={"index_html": annotated_html})
    return json.dumps(updated.model_dump(by_alias=True), ensure_ascii=False, indent=2)


def _element_pattern(element_id: str) -> re.Pattern[str]:
    escaped = re.escape(element_id)
    return re.compile(
        rf"(?P<open><(?P<tag>h[1-6]|p|a|button|label|span|section|article|div)\b"
        rf"(?=[^<>]*\bdata-forge-id\s*=\s*([\"']){escaped}\3)[^<>]*>)"
        rf"(?P<content>.*?)(?P<close></(?P=tag)\s*>)",
        re.IGNORECASE | re.DOTALL,
    )


def _read_overrides(css: str) -> dict[str, dict[str, str]]:
    block = OVERRIDE_PATTERN.search(css)
    if not block:
        return {}
    overrides: dict[str, dict[str, str]] = {}
    for rule in OVERRIDE_RULE_PATTERN.finditer(block.group(0)):
        overrides[rule.group("id")] = {
            item.group("name"): item.group("value").strip()
            for item in STYLE_DECLARATION_PATTERN.finditer(rule.group("body"))
        }
    return overrides


def _write_overrides(css: str, overrides: dict[str, dict[str, str]]) -> str:
    base = OVERRIDE_PATTERN.sub("", css).rstrip()
    rules = []
    for element_id in sorted(overrides):
        declarations = "\n".join(
            f"  {name}: {value};" for name, value in sorted(overrides[element_id].items())
        )
        if declarations:
            rules.append(f'[data-forge-id="{element_id}"] {{\n{declarations}\n}}')
    if not rules:
        return f"{base}\n"
    block = (
        "/* forge-editor-overrides:start */\n"
        + "\n\n".join(rules)
        + "\n/* forge-editor-overrides:end */"
    )
    return f"{base}\n\n{block}\n"


def apply_website_patches(files_json: str, patches: list[WebsiteElementPatch]) -> str:
    files = GeneratedWebsiteFiles.model_validate_json(ensure_editable_ids(files_json))
    index_html = files.index_html
    overrides = _read_overrides(files.style_css)

    for patch in patches:
        pattern = _element_pattern(patch.element_id)
        match = pattern.search(index_html)
        if not match:
            raise BusinessException(f"找不到可编辑元素：{patch.element_id}")

        if patch.changes.text is not None:
            if re.search(r"<[^>]+>", match.group("content")):
                raise BusinessException("包含子元素的内容暂不支持直接修改文字")
            safe_text = html.escape(patch.changes.text, quote=False)
            replacement = f"{match.group('open')}{safe_text}{match.group('close')}"
            index_html = index_html[: match.start()] + replacement + index_html[match.end() :]

        if patch.changes.styles:
            element_overrides = overrides.setdefault(patch.element_id, {})
            for name, value in patch.changes.styles.items():
                element_overrides[name] = value

    updated = files.model_copy(
        update={
            "index_html": index_html,
            "style_css": _write_overrides(files.style_css, overrides),
        }
    )
    return json.dumps(updated.model_dump(by_alias=True), ensure_ascii=False, indent=2)
