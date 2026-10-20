import assert from 'node:assert/strict'
import test from 'node:test'
import { matchesResponse, expectedResourceError } from '../sandbox/smoke_responses.mjs'

const base = 'http://127.0.0.1:5173'
const expected = { method: 'POST', path: '/api/v1/apply', status: 409 }
const response = { ...expected, url: base + expected.path }
const message = {
  text: 'Failed to load resource: the server responded with a status of 409 (Conflict)',
  url: response.url,
}

test('negative responses match exact method, query path and status', () => {
  assert.ok(matchesResponse(expected, response))
  for (const changed of [
    { method: 'GET' },
    { path: '/api/v1/apply/other' },
    { path: '/api/v1/apply?id=1' },
    { status: 500 },
  ]) {
    assert.equal(matchesResponse(expected, { ...response, ...changed }), false)
  }
  assert.equal(matchesResponse('/api/v1/apply', response), false)
  assert.ok(matchesResponse('/api/v1/apply', { ...response, status: 200 }))
})

test('only observed expected Chromium resource errors are suppressed', () => {
  assert.ok(expectedResourceError(message, [response], [expected], base))
  assert.equal(expectedResourceError(message, [], [expected], base), false)
  assert.equal(expectedResourceError(message, [response], [], base), false)
  assert.equal(
    expectedResourceError(
      { ...message, text: 'Application crashed: 409' },
      [response],
      [expected],
      base,
    ),
    false,
  )
  assert.equal(
    expectedResourceError(
      { ...message, url: base + '/api/v1/other' },
      [response],
      [expected],
      base,
    ),
    false,
  )
  assert.equal(
    expectedResourceError(
      { ...message, url: 'http://external/api/v1/apply' },
      [response],
      [expected],
      base,
    ),
    false,
  )
  assert.equal(
    expectedResourceError(
      { ...message, text: message.text.replace('409', '500') },
      [response],
      [expected],
      base,
    ),
    false,
  )
})
