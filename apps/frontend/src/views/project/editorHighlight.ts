function escapeHtml(value: string) {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function wrap(className: string, text: string) {
  return `<span class="${className}">${text}</span>`
}

export function highlightEditorCode(code: string, file: 'index.html' | 'style.css' | 'script.js') {
  const escaped = escapeHtml(code)

  if (file === 'index.html') {
    return escaped
      .replace(
        /(&lt;\/?)([\w-]+)([^&]*?)(&gt;)/g,
        (_, open, tag, rest, close) =>
          `${wrap('tok-tag', open)}${wrap('tok-name', tag)}${highlightHtmlAttrs(rest)}${wrap('tok-tag', close)}`,
      )
      .replace(/(&quot;[^&]*?&quot;)/g, (match) => wrap('tok-string', match))
      .replace(/(&#[0-9]+;)/g, (match) => wrap('tok-string', match))
  }

  if (file === 'style.css') {
    return escaped
      .replace(
        /^(\s*)([.#][\w-]+(?:[^{]*)?)(\{)/gm,
        (_, space, selector, brace) =>
          `${space}${wrap('tok-selector', selector)}${wrap('tok-tag', brace)}`,
      )
      .replace(/([\w-]+)(\s*:)/g, (_, prop, colon) => `${wrap('tok-prop', prop)}${colon}`)
      .replace(
        /(:)(\s*)([^;{}]+)/g,
        (_, colon, space, value) => `${colon}${space}${wrap('tok-string', value)}`,
      )
      .replace(/(\})/g, (match) => wrap('tok-tag', match))
  }

  return escaped
    .replace(
      /\b(import|export|from|const|let|var|function|return|if|else|for|while|new|class|extends|async|await|typeof|document|window)\b/g,
      (match) => wrap('tok-keyword', match),
    )
    .replace(/(&quot;[^&]*?&quot;|&#39;[^&]*?&#39;|`[^`]*`)/g, (match) => wrap('tok-string', match))
    .replace(/\b([A-Z][\w]*)\b/g, (match) => wrap('tok-type', match))
    .replace(/\b([a-z_$][\w$]*)(\s*\()/g, (_, name, parens) => `${wrap('tok-fn', name)}${parens}`)
}

function highlightHtmlAttrs(rest: string) {
  return rest.replace(
    /([\w-]+)(=)(&quot;[^&]*?&quot;)/g,
    (_, attr, eq, value) => `${wrap('tok-prop', attr)}${eq}${wrap('tok-string', value)}`,
  )
}
