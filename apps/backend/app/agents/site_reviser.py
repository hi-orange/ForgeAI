from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.agents.prompts.site_reviser_system import (
    SITE_REVISER_FOCUS_SYSTEM_PROMPT,
    SITE_REVISER_SYSTEM_PROMPT,
)
from app.agents.website_builder import GeneratedWebsiteFiles, strip_json_fence
from app.core.exceptions import BusinessException
from app.core.llm import chat_completion
from app.schemas.project import (
    ProjectWebsiteRevise,
    ProjectWebsiteReviseFocus,
    ProjectWebsiteReviseReply,
    WebsiteElementChanges,
    WebsiteElementPatch,
)
from app.services.website_editor import apply_website_patches

_TEXT_REWRITE_RE = re.compile(
    r"^(?:"
    r"(?:请)?(?:帮我)?(?:把|将)?(?:它|这个|这段|选中(?:的)?(?:文字|文案|标题|内容)?)?"
    r"(?:文案|文字|标题|内容)?"
    r"(?:改成|改为|换成|修改成)"
    r"|"
    r"(?:change(?:\s+it)?\s+to|rewrite\s+(?:it\s+)?(?:as|to)|set\s+(?:the\s+)?text\s+to)"
    r")"
    r"[:：\s]*"
    r"[「『\"“'\[]?"
    r"(?P<text>.+?)"
    r"[」』\"”'\]]?"
    r"\s*$",
    re.IGNORECASE | re.DOTALL,
)
_STYLE_INTENT_RE = re.compile(
    r"(颜色|配色|背景|字号|字体|字重|加粗|变大|变小|居中|对齐|间距|圆角|边距|"
    r"红色?|蓝色?|绿色?|黄色?|黑色?|白色?|灰色?|橙色?|紫色?|粉色?|透明|"
    r"color|font|size|margin|padding|bold|center|background)",
    re.IGNORECASE,
)


class _SiteReviserModelReply(BaseModel):
    mode: Literal["message", "files", "applied", "patch"] = "message"
    message: str = Field(default="", max_length=4000)
    files: GeneratedWebsiteFiles | dict[str, Any] | None = None


class _FocusPatchModelReply(BaseModel):
    mode: Literal["message", "patch"] = "message"
    message: str = Field(default="", max_length=4000)
    text: str | None = Field(default=None, max_length=2000)
    styles: dict[str, str] = Field(default_factory=dict)


def _build_messages(
    payload: ProjectWebsiteRevise,
    *,
    current_files: str,
    approved_spec: str | None,
) -> list[dict[str, Any]]:
    try:
        files_payload: Any = json.loads(current_files)
    except json.JSONDecodeError:
        files_payload = current_files
    user_payload: dict[str, Any] = {
        "instruction": payload.instruction,
        "approved_spec": approved_spec,
        "current_files": files_payload,
    }
    if payload.focus is not None:
        user_payload["focus"] = {
            "element_id": payload.focus.element_id,
            "tag_name": payload.focus.tag_name,
            "text": payload.focus.text,
            "text_editable": payload.focus.text_editable,
            "current_styles": payload.focus.styles,
        }
    messages: list[dict[str, Any]] = [{"role": "system", "content": SITE_REVISER_SYSTEM_PROMPT}]
    for item in payload.history:
        messages.append({"role": item.role, "content": item.content})
    messages.append(
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, indent=2),
        }
    )
    return messages


def _build_focus_messages(payload: ProjectWebsiteRevise) -> list[dict[str, Any]]:
    focus = payload.focus
    assert focus is not None
    user_payload = {
        "element": {
            "element_id": focus.element_id,
            "tag_name": focus.tag_name,
            "text": focus.text,
            "text_editable": focus.text_editable,
            "current_styles": focus.styles,
        },
        "instruction": payload.instruction,
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SITE_REVISER_FOCUS_SYSTEM_PROMPT}
    ]
    for item in payload.history:
        messages.append({"role": item.role, "content": item.content})
    messages.append(
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, indent=2),
        }
    )
    return messages


def _parse_files(
    raw: GeneratedWebsiteFiles | dict[str, Any] | None,
) -> GeneratedWebsiteFiles | None:
    if raw is None:
        return None
    if isinstance(raw, GeneratedWebsiteFiles):
        return raw
    try:
        return GeneratedWebsiteFiles.model_validate(raw)
    except ValidationError:
        return None


def _parse_model_reply(content: str) -> _SiteReviserModelReply:
    stripped = strip_json_fence(content)
    try:
        return _SiteReviserModelReply.model_validate_json(stripped)
    except (ValidationError, ValueError, json.JSONDecodeError):
        pass
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise BusinessException("网站修改助手返回格式不对") from exc
    if not isinstance(data, dict):
        raise BusinessException("网站修改助手返回格式不对")
    mode = data.get("mode")
    if mode in {"applied", "patch"}:
        data["mode"] = "files" if data.get("files") else "message"
    try:
        return _SiteReviserModelReply.model_validate(data)
    except ValidationError as exc:
        message = str(data.get("message") or "").strip()
        if message:
            return _SiteReviserModelReply(mode="message", message=message)
        raise BusinessException("网站修改助手返回格式不对") from exc


def _parse_focus_patch_reply(content: str) -> _FocusPatchModelReply:
    stripped = strip_json_fence(content)
    try:
        return _FocusPatchModelReply.model_validate_json(stripped)
    except (ValidationError, ValueError, json.JSONDecodeError):
        pass
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise BusinessException("网站修改助手返回格式不对") from exc
    if not isinstance(data, dict):
        raise BusinessException("网站修改助手返回格式不对")
    try:
        return _FocusPatchModelReply.model_validate(data)
    except ValidationError as exc:
        message = str(data.get("message") or "").strip()
        if message:
            return _FocusPatchModelReply(mode="message", message=message)
        raise BusinessException("网站修改助手返回格式不对") from exc


def _extract_clear_text_rewrite(instruction: str) -> str | None:
    cleaned = instruction.strip()
    if not cleaned:
        return None
    match = _TEXT_REWRITE_RE.match(cleaned)
    if not match:
        return None
    text = match.group("text").strip().strip("「」『』\"“”'[]")
    if not text or _STYLE_INTENT_RE.search(text):
        return None
    return text[:2000]


def _applied_from_patch(
    *,
    current_files: str,
    focus: ProjectWebsiteReviseFocus,
    changes: WebsiteElementChanges,
    message: str,
) -> ProjectWebsiteReviseReply:
    if changes.text is None and not changes.styles:
        return ProjectWebsiteReviseReply(
            mode="message",
            message="好像没什么变化，换个更具体的说法试试？",
        )
    if changes.text is not None and not focus.text_editable:
        return ProjectWebsiteReviseReply(
            mode="message",
            message="这个元素里还有子内容，我没法直接整段换文字。换个说法，或选中里面的文字再试？",
        )
    if (
        changes.text is not None
        and changes.text.strip() == (focus.text or "").strip()
        and not changes.styles
    ):
        return ProjectWebsiteReviseReply(
            mode="message",
            message="文案好像已经是这样了，换个更具体的说法试试？",
        )

    try:
        updated = apply_website_patches(
            current_files,
            [WebsiteElementPatch(element_id=focus.element_id, changes=changes)],
        )
    except BusinessException as exc:
        return ProjectWebsiteReviseReply(
            mode="message",
            message=str(exc.msg) if exc.msg else "这次没改成功，换个说法再试一次？",
        )

    return ProjectWebsiteReviseReply(
        mode="applied",
        message=message.strip() or "好，已改好。",
        files_json=updated,
    )


def _try_deterministic_focus_revise(
    payload: ProjectWebsiteRevise,
    *,
    current_files: str,
) -> ProjectWebsiteReviseReply | None:
    focus = payload.focus
    if focus is None or not focus.text_editable:
        return None
    new_text = _extract_clear_text_rewrite(payload.instruction)
    if new_text is None:
        return None
    return _applied_from_patch(
        current_files=current_files,
        focus=focus,
        changes=WebsiteElementChanges(text=new_text),
        message=f"好，已改成「{new_text}」。",
    )


def _suggest_focus_patch_reply(
    payload: ProjectWebsiteRevise,
    *,
    current_files: str,
) -> ProjectWebsiteReviseReply:
    focus = payload.focus
    assert focus is not None

    content = chat_completion(
        messages=_build_focus_messages(payload),
        temperature=0.1,
        max_tokens=1000,
        json_output=True,
    )
    try:
        raw = _parse_focus_patch_reply(content)
    except BusinessException:
        return ProjectWebsiteReviseReply(
            mode="message",
            message="没听清你想怎么改，再说具体一点？",
        )

    message = (raw.message or "").strip() or "好的。"
    if raw.mode == "message":
        return ProjectWebsiteReviseReply(mode="message", message=message)

    try:
        changes = WebsiteElementChanges.model_validate(
            {"text": raw.text, "styles": raw.styles or {}}
        )
    except ValidationError:
        return ProjectWebsiteReviseReply(
            mode="message",
            message="这次想法有点飘，换个更具体的改法试试？",
        )

    return _applied_from_patch(
        current_files=current_files,
        focus=focus,
        changes=changes,
        message=message,
    )


def _suggest_full_site_revise_reply(
    payload: ProjectWebsiteRevise,
    *,
    current_files: str,
    approved_spec: str | None,
    current: GeneratedWebsiteFiles,
) -> ProjectWebsiteReviseReply:
    content = chat_completion(
        messages=_build_messages(
            payload,
            current_files=current_files,
            approved_spec=approved_spec,
        ),
        temperature=0.2,
        max_tokens=16384,
        json_output=True,
    )
    try:
        raw = _parse_model_reply(content)
    except BusinessException:
        return ProjectWebsiteReviseReply(
            mode="message",
            message="没听清你想怎么改，再说具体一点？",
        )

    message = (raw.message or "").strip() or "好的。"
    files = _parse_files(raw.files)
    if raw.mode == "message" or files is None:
        return ProjectWebsiteReviseReply(mode="message", message=message)

    if (
        files.index_html == current.index_html
        and files.style_css == current.style_css
        and files.script_js == current.script_js
    ):
        return ProjectWebsiteReviseReply(
            mode="message",
            message=(
                message
                if ("？" in message or "?" in message)
                else "好像没什么变化，换个更具体的说法试试？"
            ),
        )

    html = files.index_html.lower()
    if "<!doctype html" not in html or "<body" not in html:
        return ProjectWebsiteReviseReply(
            mode="message",
            message="这次生成的页面不完整，我先没保存。再描述一下你想要的效果吧。",
        )

    updated = json.dumps(files.model_dump(by_alias=True), ensure_ascii=False, indent=2)
    return ProjectWebsiteReviseReply(
        mode="applied",
        message=message,
        files_json=updated,
    )


def suggest_site_revise_reply(
    payload: ProjectWebsiteRevise,
    *,
    current_files: str,
    approved_spec: str | None,
) -> ProjectWebsiteReviseReply:
    try:
        current = GeneratedWebsiteFiles.model_validate_json(current_files)
    except (ValidationError, ValueError) as exc:
        raise BusinessException("当前网站文件格式无效，没法改") from exc

    # Design Ask with a selected element: patch that node instead of rewriting all files.
    if payload.focus is not None:
        deterministic = _try_deterministic_focus_revise(payload, current_files=current_files)
        if deterministic is not None:
            return deterministic
        return _suggest_focus_patch_reply(payload, current_files=current_files)

    return _suggest_full_site_revise_reply(
        payload,
        current_files=current_files,
        approved_spec=approved_spec,
        current=current,
    )
