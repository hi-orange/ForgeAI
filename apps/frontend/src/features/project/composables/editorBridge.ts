export const EDITOR_BRIDGE_SCRIPT = String.raw`

(() => {
  const SOURCE = 'forge-visual-editor';
  let hovered = null;
  let selected = null;
  let editing = null;
  let textBeforeEditing = '';
  const editableSelector = 'h1,h2,h3,h4,h5,h6,p,a,button,label,span,section,article,div';
  const usedIds = new Set([...document.querySelectorAll('[data-forge-id]')]
    .map((element) => element.dataset.forgeId));
  const counters = {};
  document.querySelectorAll(editableSelector).forEach((element) => {
    if (element.dataset.forgeId) return;
    const tag = element.tagName.toLowerCase();
    counters[tag] = (counters[tag] || 0) + 1;
    let candidate = 'forge-' + tag + '-' + counters[tag];
    while (usedIds.has(candidate)) {
      counters[tag] += 1;
      candidate = 'forge-' + tag + '-' + counters[tag];
    }
    usedIds.add(candidate);
    element.dataset.forgeId = candidate;
  });
  const style = document.createElement('style');
  style.textContent = '[data-forge-id].forge-editor-hover{outline:1px solid #60a5fa!important;outline-offset:1px;cursor:pointer!important}[data-forge-id].forge-editor-text-editing{outline:2px solid #2563eb!important;outline-offset:2px;cursor:text!important;caret-color:#2563eb}';
  document.head.appendChild(style);

  const editableFrom = (target) => target instanceof Element ? target.closest('[data-forge-id]') : null;
  const toHex = (value) => {
    if (/rgba\([^)]*,\s*0(?:\.0+)?\s*\)/i.test(String(value))) return '#ffffff';
    const match = String(value).match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/i);
    if (!match) return /^#[0-9a-f]{6}$/i.test(value) ? value : '#ffffff';
    return '#' + [match[1], match[2], match[3]].map((part) => Number(part).toString(16).padStart(2, '0')).join('');
  };
  const pxValue = (value) => {
    const number = Number.parseFloat(value);
    return Number.isFinite(number) ? number + 'px' : '0px';
  };
  const fontValue = (value) => {
    const normalized = String(value).toLowerCase();
    if (normalized.includes('inter')) return 'Inter';
    if (normalized.includes('georgia')) return 'Georgia';
    if (normalized.includes('arial')) return 'Arial';
    if (normalized.includes('serif') && !normalized.includes('sans')) return 'serif';
    if (normalized.includes('system')) return 'system-ui';
    return 'sans-serif';
  };
  const readRect = (element) => {
    const rect = element.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  };
  const publishSelection = (element) => {
    const computed = getComputedStyle(element);
    window.parent.postMessage({
      source: SOURCE,
      type: 'element-selected',
      payload: {
        elementId: element.dataset.forgeId,
        tagName: element.tagName,
        text: element.children.length ? '' : element.textContent.trim(),
        textEditable: element.children.length === 0,
        rect: readRect(element),
        styles: {
          color: toHex(computed.color),
          'background-color': toHex(computed.backgroundColor),
          'border-color': toHex(computed.borderColor),
          'font-size': pxValue(computed.fontSize),
          'font-weight': /^\d+$/.test(computed.fontWeight) ? computed.fontWeight : '400',
          'font-family': fontValue(computed.fontFamily),
          'text-align': ['left', 'center', 'right', 'justify'].includes(computed.textAlign) ? computed.textAlign : 'left',
          'margin-top': pxValue(computed.marginTop),
          'margin-right': pxValue(computed.marginRight),
          'margin-bottom': pxValue(computed.marginBottom),
          'margin-left': pxValue(computed.marginLeft),
          'padding-top': pxValue(computed.paddingTop),
          'padding-right': pxValue(computed.paddingRight),
          'padding-bottom': pxValue(computed.paddingBottom),
          'padding-left': pxValue(computed.paddingLeft),
          gap: pxValue(computed.gap),
          'border-radius': pxValue(computed.borderRadius),
        },
      },
    }, '*');
  };
  const publishHover = (element) => {
    window.parent.postMessage({
      source: SOURCE,
      type: 'element-hovered',
      payload: {
        elementId: element.dataset.forgeId,
        tagName: element.tagName,
        rect: readRect(element),
      },
    }, '*');
  };
  const publishTextChange = (element) => {
    window.parent.postMessage({
      source: SOURCE,
      type: 'element-changed',
      payload: {
        elementId: element.dataset.forgeId,
        text: element.textContent || '',
        rect: readRect(element),
      },
    }, '*');
  };
  const textObserver = new MutationObserver(() => {
    if (editing) publishTextChange(editing);
  });
  textObserver.observe(document.body, {
    subtree: true,
    childList: true,
    characterData: true,
  });
  const finishTextEditing = (restore = false) => {
    if (!editing) return;
    if (restore) editing.textContent = textBeforeEditing;
    editing.removeAttribute('contenteditable');
    editing.classList.remove('forge-editor-text-editing');
    publishTextChange(editing);
    publishSelection(editing);
    editing = null;
  };

  document.addEventListener('mouseover', (event) => {
    const element = editableFrom(event.target);
    if (hovered && hovered !== selected) hovered.classList.remove('forge-editor-hover');
    hovered = element;
    if (hovered && hovered !== selected) {
      hovered.classList.add('forge-editor-hover');
      publishHover(hovered);
    }
  }, true);
  document.addEventListener('mouseout', (event) => {
    const element = editableFrom(event.target);
    const related = editableFrom(event.relatedTarget);
    if (element && related === element) return;
    if (element && element !== selected) element.classList.remove('forge-editor-hover');
    window.parent.postMessage({
      source: SOURCE,
      type: 'element-hover-cleared',
      payload: { elementId: element?.dataset.forgeId || '' },
    }, '*');
  }, true);
  document.addEventListener('click', (event) => {
    const element = editableFrom(event.target);
    if (!element) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (selected) selected.classList.remove('forge-editor-hover');
    selected = element;
    hovered = null;
    publishSelection(selected);
  }, true);
  document.addEventListener('dblclick', (event) => {
    const element = editableFrom(event.target);
    if (!element || element.children.length > 0) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (editing && editing !== element) finishTextEditing();
    selected = element;
    editing = element;
    textBeforeEditing = element.textContent;
    element.setAttribute('contenteditable', 'plaintext-only');
    element.classList.add('forge-editor-text-editing');
    element.focus();
    const range = document.createRange();
    range.selectNodeContents(element);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    publishSelection(element);
  }, true);
  document.addEventListener('input', (event) => {
    if (editing && event.target === editing) publishTextChange(editing);
  }, true);
  document.addEventListener('keyup', (event) => {
    if (editing && event.target === editing) publishTextChange(editing);
  }, true);
  document.addEventListener('compositionend', (event) => {
    if (editing && event.target === editing) publishTextChange(editing);
  }, true);
  document.addEventListener('focusout', (event) => {
    if (editing && event.target === editing) finishTextEditing();
  }, true);
  document.addEventListener('keydown', (event) => {
    if (!editing || event.target !== editing) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      finishTextEditing(true);
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      finishTextEditing();
    }
  }, true);
  const publishPosition = () => {
    if (selected) {
      window.parent.postMessage({
        source: SOURCE,
        type: 'selection-position',
        payload: { elementId: selected.dataset.forgeId, rect: readRect(selected) },
      }, '*');
    } else if (hovered) {
      publishHover(hovered);
    }
  };
  window.addEventListener('scroll', publishPosition, true);
  window.addEventListener('resize', publishPosition);
  document.addEventListener('submit', (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);

  window.addEventListener('message', (event) => {
    const message = event.data;
    if (!message || message.source !== 'forge-visual-editor-host') return;
    if (message.type === 'clear-selection') {
      finishTextEditing();
      if (selected) selected.classList.remove('forge-editor-hover');
      selected = null;
      return;
    }
    if (message.type !== 'update-element' || !message.payload) return;
    const element = [...document.querySelectorAll('[data-forge-id]')]
      .find((candidate) => candidate.dataset.forgeId === message.payload.elementId);
    if (!element) return;
    const changes = message.payload.changes || {};
    if (typeof changes.text === 'string' && element.children.length === 0) {
      element.textContent = changes.text;
    }
    const styles = changes.styles || {};
    const allowedStyles = new Set([
      'color', 'background-color', 'border-color', 'font-size', 'font-weight', 'font-family',
      'text-align', 'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
      'padding-top', 'padding-right', 'padding-bottom', 'padding-left', 'gap', 'border-radius'
    ]);
    Object.entries(styles).forEach(([name, value]) => {
      if (allowedStyles.has(name) && typeof value === 'string') element.style.setProperty(name, value);
    });
    requestAnimationFrame(() => publishSelection(element));
  });
})();
`
