import assert from 'node:assert/strict'
import test from 'node:test'
import { runBrowserActions } from '../sandbox/probe_actions.mjs'

test('browser actions run in order and settle once before result checks', async () => {
  const events = []
  const page = {
    waitForLoadState: async (state, options) => {
      events.push(['settle', state, options.timeout])
    },
  }
  const handlers = {
    click: async (_page, action) => events.push(['click', action.selector]),
    wait_for_selector: async (_page, action) =>
      events.push(['wait_for_selector', action.selector]),
    fill: async (_page, action) => events.push(['fill', action.selector]),
  }

  await runBrowserActions(
    page,
    [
      { type: 'click', selector: '#login' },
      { type: 'wait_for_selector', selector: '#form' },
      { type: 'fill', selector: '#email' },
    ],
    handlers,
  )

  assert.deepEqual(events, [
    ['click', '#login'],
    ['wait_for_selector', '#form'],
    ['fill', '#email'],
    ['settle', 'networkidle', 5000],
  ])
})

test('empty action groups do not add a second idle wait', async () => {
  const page = { waitForLoadState: () => assert.fail('unexpected idle wait') }
  await runBrowserActions(page, [], {})
})

test('idle timeout remains nonfatal, while action errors still fail the check', async () => {
  const page = { waitForLoadState: async () => { throw new Error('idle timeout') } }
  await runBrowserActions(page, [{ type: 'click' }], { click: async () => {} })
  await assert.rejects(
    runBrowserActions(page, [{ type: 'click' }], { click: async () => { throw new Error('click failed') } }),
    /click failed/,
  )
  await assert.rejects(runBrowserActions(page, [{ type: 'unknown' }], {}), /unsupported browser action/)
})
