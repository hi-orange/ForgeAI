// Exact negative-response matching shared by the runner and its contract tests.
export function matchesResponse(expected, response) {
  if (typeof expected === 'string') {
    return response.path.split('?')[0].startsWith(expected) && response.status < 400
  }
  return (
    expected.method === response.method &&
    expected.path === response.path &&
    expected.status === response.status
  )
}

export function expectedResourceError(message, responses, expectations, baseUrl) {
  // Suppress only Chromium's resource failure for an observed, explicitly expected
  // error response. Application console.error and pageerror remain failures.
  const match =
    /^Failed to load resource: the server responded with a status of (\d{3})(?: \([^\n]*\))?$/.exec(
      message.text,
    )
  if (!match) return false
  let url
  try {
    url = new URL(message.url)
  } catch {
    return false
  }
  if (url.origin !== new URL(baseUrl).origin) return false
  return responses.some(
    (response) =>
      response.url === url.href &&
      response.status === Number(match[1]) &&
      expectations.some(
        (expected) =>
          typeof expected === 'object' &&
          expected.status >= 400 &&
          matchesResponse(expected, response),
      ),
  )
}
