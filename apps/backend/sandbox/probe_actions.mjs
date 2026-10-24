export async function runBrowserActions(page, actions, actionHandlers) {
  for (const action of actions) {
    const handler = actionHandlers[action.type]
    if (!handler) throw new Error('unsupported browser action: ' + action.type)
    await handler(page, action)
  }
  if (actions.length) {
    await page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => {})
  }
}
