"""Persistent design overlays and the preview-side editing bridge."""

# The embedded JavaScript is served verbatim to preview documents.
# ruff: noqa: E501

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.core.settings import settings
from app.generation.workspace import default_workspace_path, workspace_is_ready
from app.models.user import User
from app.services import project as project_service
from app.services.requirements import get_requirements_status

DesignStyleProperty = Literal[
    "margin",
    "padding",
    "backgroundColor",
    "color",
    "opacity",
    "borderRadius",
    "fontFamily",
    "fontWeight",
    "fontSize",
    "textAlign",
    "textDecoration",
]


class PreviewTheme(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_id: str = Field(default="forge", min_length=1, max_length=64)
    name: str = Field(default="Forge", min_length=1, max_length=80)
    background: str = Field(default="#f8fafc", min_length=1, max_length=32)
    surface: str = Field(default="#ffffff", min_length=1, max_length=32)
    text: str = Field(default="#0f172a", min_length=1, max_length=32)
    primary: str = Field(default="#2563eb", min_length=1, max_length=32)
    muted: str = Field(default="#64748b", min_length=1, max_length=32)
    border: str = Field(default="#e2e8f0", min_length=1, max_length=32)
    font_family: str = Field(default="Inter, system-ui, sans-serif", min_length=1, max_length=160)
    radius: int = Field(default=12, ge=0, le=48)
    shadow: str = Field(default="0 12px 32px rgba(15, 23, 42, 0.08)", max_length=160)

    @field_validator("background", "surface", "text", "primary", "muted", "border")
    @classmethod
    def validate_color(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", normalized):
            raise ValueError("theme colors must use hexadecimal notation")
        return normalized

    @field_validator("font_family", "shadow")
    @classmethod
    def validate_css_value(cls, value: str) -> str:
        normalized = value.strip()
        lowered = normalized.lower()
        if any(token in lowered for token in ("url(", "expression(", "javascript:")):
            raise ValueError("unsafe CSS value")
        if any(token in normalized for token in ("<", ">", "{", "}")):
            raise ValueError("unsafe CSS value")
        return normalized


class PreviewElementOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str = Field(min_length=1, max_length=512)
    label: str = Field(default="Element", max_length=120)
    styles: dict[DesignStyleProperty, str] = Field(default_factory=dict)
    text: str | None = Field(default=None, max_length=4000)
    image_url: str | None = Field(default=None, max_length=5_700_000)

    @field_validator("selector")
    @classmethod
    def validate_selector(cls, value: str) -> str:
        normalized = value.strip()
        # The preview bridge generates paths with the standard CSS child
        # combinator (for example, body > main > h1). The greater-than sign is
        # therefore expected input, not markup. Keep breakout characters and
        # unsupported control characters rejected.
        if any(token in normalized for token in ("<", "{", "}")) or any(
            ord(character) < 32 and character not in {"\t", "\n", "\r"} for character in normalized
        ):
            raise ValueError("invalid selector")
        return normalized

    @field_validator("styles")
    @classmethod
    def validate_styles(
        cls, styles: dict[DesignStyleProperty, str]
    ) -> dict[DesignStyleProperty, str]:
        for value in styles.values():
            if len(value) > 160:
                raise ValueError("style value is too long")
            lowered = value.lower()
            if any(token in lowered for token in ("url(", "expression(", "javascript:")):
                raise ValueError("unsafe style value")
            if any(token in value for token in ("<", ">", "{", "}")):
                raise ValueError("unsafe style value")
        return styles

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized.startswith("data:image/"):
            if not re.match(r"^data:image/(png|jpeg|gif|webp|bmp);base64,", normalized):
                raise ValueError("unsupported inline image")
            return normalized
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("image URL must use http or https")
        return normalized


class PreviewDesignUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: PreviewTheme = Field(default_factory=PreviewTheme)
    elements: list[PreviewElementOverride] = Field(default_factory=list, max_length=200)


class PreviewDesignState(PreviewDesignUpdate):
    revision: int = Field(default=0, ge=0)
    saved_at: datetime | None = None


_design_locks: dict[str, threading.Lock] = {}
_design_locks_guard = threading.Lock()


def _design_lock(workspace: Path) -> threading.Lock:
    key = str(workspace.resolve())
    with _design_locks_guard:
        return _design_locks.setdefault(key, threading.Lock())


def _design_root(workspace: Path) -> Path:
    root = workspace / "forgeai" / "design"
    try:
        root.resolve().relative_to(workspace.resolve())
    except ValueError as exc:
        raise BusinessException("项目设计目录不安全，无法访问") from exc
    return root


def _read_design_state(workspace: Path) -> PreviewDesignState:
    current = _design_root(workspace) / "current.json"
    if not current.is_file():
        return PreviewDesignState()
    try:
        return PreviewDesignState.model_validate_json(current.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BusinessException("项目设计状态已损坏，无法读取") from exc


def _completed_workspace(db: Session, user: User, project_id: int) -> Path:
    project_service.get_user_project(db, user, project_id)
    status = get_requirements_status(db, user, project_id)
    if status.state != "completed" or not status.run_id:
        raise BusinessException("只有独立验收通过的构建才能使用 Design")
    if not workspace_is_ready(project_id, status.run_id):
        raise BusinessException("工作区尚未就绪，无法使用 Design")
    workspace = default_workspace_path(settings.runtime_data_root, project_id, status.run_id)
    if not workspace.is_dir():
        raise BusinessException("工作区目录不存在")
    return workspace


def get_design_state(db: Session, user: User, project_id: int) -> PreviewDesignState:
    return _read_design_state(_completed_workspace(db, user, project_id))


def save_design_state(
    db: Session,
    user: User,
    project_id: int,
    update: PreviewDesignUpdate,
) -> PreviewDesignState:
    workspace = _completed_workspace(db, user, project_id)
    with _design_lock(workspace):
        previous = _read_design_state(workspace)
        root = _design_root(workspace)
        revisions = root / "revisions"
        revisions.mkdir(parents=True, exist_ok=True)
        revision = previous.revision + 1
        while True:
            saved = PreviewDesignState(
                revision=revision,
                saved_at=datetime.now(UTC),
                theme=update.theme,
                elements=update.elements,
            )
            payload = saved.model_dump_json(indent=2) + "\n"
            revision_path = revisions / f"{saved.revision:06d}.json"
            try:
                with revision_path.open("x", encoding="utf-8") as revision_file:
                    revision_file.write(payload)
                break
            except FileExistsError:
                revision += 1
        current = root / "current.json"
        temporary = root / f".current.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(current)
        finally:
            temporary.unlink(missing_ok=True)
        return saved


_DESIGN_BRIDGE = r"""
(() => {
  const channel = 'forgeai-preview-design';
  let enabled = false;
  let state = window.__FORGEAI_DESIGN_STATE__ || { theme: {}, elements: [] };
  let selected = null;
  const overlay = document.createElement('div');
  const badge = document.createElement('div');

  Object.assign(overlay.style, {
    position: 'fixed', zIndex: '2147483646', pointerEvents: 'none', display: 'none',
    border: '2px solid #6366f1', borderRadius: '4px', boxSizing: 'border-box'
  });
  Object.assign(badge.style, {
    position: 'fixed', zIndex: '2147483647', pointerEvents: 'none', display: 'none',
    padding: '4px 7px', borderRadius: '5px', background: '#4f46e5', color: '#fff',
    font: '500 11px/1.2 system-ui, sans-serif', boxShadow: '0 4px 12px rgba(0,0,0,.18)'
  });
  document.documentElement.append(overlay, badge);
  const scrollbarStyle = document.createElement('style');
  scrollbarStyle.textContent = `
    html { scrollbar-width: thin; scrollbar-color: transparent transparent; }
    html:hover { scrollbar-color: rgba(113, 113, 122, .42) transparent; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { border-radius: 999px; background: transparent; }
    html:hover::-webkit-scrollbar-thumb { background: rgba(113, 113, 122, .42); }
  `;
  document.head.append(scrollbarStyle);

  const post = (type, payload = {}) => {
    window.parent.postMessage({ channel, type, ...payload }, '*');
  };
  const escapeSelector = (value) => window.CSS?.escape ? window.CSS.escape(value) : value.replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  const selectorFor = (element) => {
    if (element.id) return `#${escapeSelector(element.id)}`;
    for (const key of ['data-forgeai-id', 'data-testid']) {
      const value = element.getAttribute(key);
      if (value) return `[${key}="${escapeSelector(value)}"]`;
    }
    const parts = [];
    let current = element;
    while (current && current !== document.body && parts.length < 8) {
      const tag = current.tagName.toLowerCase();
      const siblings = current.parentElement
        ? [...current.parentElement.children].filter((node) => node.tagName === current.tagName)
        : [];
      const index = siblings.indexOf(current) + 1;
      parts.unshift(`${tag}:nth-of-type(${Math.max(index, 1)})`);
      current = current.parentElement;
    }
    return `body > ${parts.join(' > ')}`;
  };
  const elementLabel = (element) => {
    const text = (element.getAttribute('aria-label') || element.textContent || '').trim().replace(/\s+/g, ' ');
    return text ? `${element.tagName.toLowerCase()} · ${text.slice(0, 36)}` : element.tagName.toLowerCase();
  };
  const readSelection = (element) => {
    const styles = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return {
      selector: selectorFor(element), label: elementLabel(element), tag: element.tagName.toLowerCase(),
      text: element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement
        ? element.value : (element.textContent || '').trim().slice(0, 4000),
      image_url: element instanceof HTMLImageElement ? element.currentSrc || element.src : null,
      class_name: typeof element.className === 'string' ? element.className : '',
      rect: { width: Math.round(rect.width), height: Math.round(rect.height) },
      styles: {
        margin: styles.margin, padding: styles.padding, backgroundColor: styles.backgroundColor,
        color: styles.color, opacity: styles.opacity, borderRadius: styles.borderRadius,
        fontFamily: styles.fontFamily, fontWeight: styles.fontWeight, fontSize: styles.fontSize,
        textAlign: styles.textAlign, textDecoration: styles.textDecorationLine
      }
    };
  };
  const positionOverlay = () => {
    if (!enabled || !selected || !selected.isConnected) {
      overlay.style.display = 'none'; badge.style.display = 'none'; return;
    }
    const rect = selected.getBoundingClientRect();
    Object.assign(overlay.style, {
      display: 'block', left: `${rect.left}px`, top: `${rect.top}px`,
      width: `${rect.width}px`, height: `${rect.height}px`
    });
    badge.textContent = `${elementLabel(selected)} · ${Math.round(rect.width)}×${Math.round(rect.height)}`;
    Object.assign(badge.style, {
      display: 'block', left: `${Math.max(4, rect.left)}px`, top: `${Math.max(4, rect.top - 25)}px`
    });
  };
  const applyTheme = (theme = {}) => {
    const root = document.documentElement;
    const variables = {
      '--background': theme.background, '--color-background': theme.background,
      '--card': theme.surface, '--color-card': theme.surface,
      '--foreground': theme.text, '--color-foreground': theme.text,
      '--primary': theme.primary, '--color-primary': theme.primary,
      '--muted-foreground': theme.muted, '--color-muted-foreground': theme.muted,
      '--border': theme.border, '--color-border': theme.border,
      '--radius': `${theme.radius || 0}px`
    };
    for (const [name, value] of Object.entries(variables)) if (value) root.style.setProperty(name, value);
    if (theme.background) document.body.style.backgroundColor = theme.background;
    if (theme.text) document.body.style.color = theme.text;
    if (theme.font_family) document.body.style.fontFamily = theme.font_family;
  };
  const applyOverride = (override) => {
    let element;
    try { element = document.querySelector(override.selector); } catch { return; }
    if (!element) return;
    for (const [name, value] of Object.entries(override.styles || {})) element.style[name] = value;
    if (override.text !== null && override.text !== undefined) {
      if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
        if (element.value !== override.text) element.value = override.text;
      } else if (element.textContent !== override.text) element.textContent = override.text;
    }
    if (override.image_url) {
      if (element instanceof HTMLImageElement) {
        if (element.src !== override.image_url) element.src = override.image_url;
      }
      else {
        element.style.backgroundImage = `url(${JSON.stringify(override.image_url)})`;
        element.style.backgroundSize = 'cover'; element.style.backgroundPosition = 'center';
      }
    }
  };
  const applyState = (next) => {
    state = next || { theme: {}, elements: [] };
    applyTheme(state.theme || {});
    for (const override of state.elements || []) applyOverride(override);
    requestAnimationFrame(positionOverlay);
  };
  const pages = () => {
    const found = new Map([[location.pathname + location.search + location.hash, document.title || '当前页面']]);
    for (const anchor of document.querySelectorAll('a[href]')) {
      try {
        const url = new URL(anchor.href, location.href);
        if (url.origin !== location.origin) continue;
        const path = url.pathname + url.search + url.hash;
        found.set(path, (anchor.textContent || path).trim().slice(0, 60) || path);
      } catch {}
    }
    return [...found].map(([path, label]) => ({ path, label }));
  };
  const reportRoute = () => post('route', {
    path: location.pathname + location.search + location.hash, pages: pages()
  });
  const select = (element) => {
    selected = element;
    positionOverlay();
    post('selection', { selection: readSelection(element) });
  };

  document.addEventListener('click', (event) => {
    if (!enabled) return;
    const target = event.target instanceof Element ? event.target : null;
    if (!target || target === overlay || target === badge) return;
    event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation();
    select(target);
  }, true);
  window.addEventListener('scroll', positionOverlay, true);
  window.addEventListener('resize', positionOverlay);
  window.addEventListener('error', (event) => post('console', {
    level: 'error', message: event.message || 'Page error'
  }));
  window.addEventListener('unhandledrejection', (event) => post('console', {
    level: 'error', message: String(event.reason || 'Unhandled promise rejection').slice(0, 1000)
  }));
  for (const level of ['log', 'info', 'warn', 'error']) {
    const original = console[level].bind(console);
    console[level] = (...args) => {
      original(...args);
      post('console', { level, message: args.map((item) => {
        try { return typeof item === 'string' ? item : JSON.stringify(item); }
        catch { return String(item); }
      }).join(' ').slice(0, 1000) });
    };
  }
  for (const method of ['pushState', 'replaceState']) {
    const original = history[method].bind(history);
    history[method] = (...args) => { const result = original(...args); setTimeout(reportRoute); return result; };
  }
  window.addEventListener('popstate', reportRoute);
  window.addEventListener('message', (event) => {
    if (event.source !== window.parent || event.data?.channel !== channel) return;
    if (event.data.type === 'design-mode') {
      enabled = Boolean(event.data.enabled);
      document.documentElement.style.cursor = enabled ? 'crosshair' : '';
      if (!enabled) { selected = null; post('selection', { selection: null }); }
      positionOverlay();
    } else if (event.data.type === 'apply-state') {
      applyState(event.data.state);
    } else if (event.data.type === 'navigate' && typeof event.data.path === 'string') {
      location.href = event.data.path;
    } else if (event.data.type === 'request-state') {
      reportRoute();
      if (selected) post('selection', { selection: readSelection(selected) });
    }
  });

  applyState(state);
  new MutationObserver(() => { applyState(state); positionOverlay(); }).observe(document.body, {
    childList: true, subtree: true
  });
  post('ready', { path: location.pathname + location.search + location.hash, pages: pages() });
})();
"""


def inject_design_bridge(data: bytes, workspace: Path) -> bytes:
    """Inject the trusted design bridge into HTML served by the local preview proxy."""

    try:
        html = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    state_json = json.dumps(
        _read_design_state(workspace).model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    state_json = (
        state_json.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    )
    payload = (
        "<script>window.__FORGEAI_DESIGN_STATE__="
        + state_json
        + ";</script><script>"
        + _DESIGN_BRIDGE
        + "</script>"
    )
    marker = "</body>"
    if marker in html:
        html = html.replace(marker, payload + marker, 1)
    else:
        html += payload
    return html.encode("utf-8")
