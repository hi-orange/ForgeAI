// Trusted browser assertions. No model JavaScript, evaluate, shell or external URLs.
import playwright from '/opt/visual/node_modules/playwright-core/index.js'

let input = ''
for await (const chunk of process.stdin) input += chunk
const steps = JSON.parse(input)
const observations = []
let browser
let status = 'passed'
let failureKind = null
let evidence = ''
let activeAction = null
const errors = []
try {
  browser = await playwright.chromium.launch({
    executablePath: '/usr/bin/chromium', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  })
  const context = await browser.newContext({ serviceWorkers: 'block' })
  await context.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.hostname === '127.0.0.1' && ['8000', '5173'].includes(url.port)) await route.continue()
    else await route.abort('blockedbyclient')
  })
  const page = await context.newPage()
  page.on('pageerror', error => { if (errors.length < 10) errors.push(String(error)) })
  page.on('console', message => {
    if (message.type() === 'error' && errors.length < 10) errors.push(message.text().slice(0, 500))
  })
  page.on('requestfailed', request => {
    if (errors.length < 10) errors.push(request.url() + ': ' + request.failure()?.errorText)
  })
  page.setDefaultTimeout(15000)
  page.setDefaultNavigationTimeout(30000)
  for (let index = 0; index < steps.length; index++) {
    const step = steps[index]
    activeAction = step.action
    const locator = step.selector ? page.locator(step.selector) : null
    if (step.path && (!step.path.startsWith('/') || step.path.startsWith('//') || /[\\\x00-\x1f]/.test(step.path))) {
      throw new Error('script: path must stay inside the isolated application')
    }
    switch (step.action) {
      case 'navigate': await page.goto('http://127.0.0.1:5173' + step.path, { waitUntil: 'domcontentloaded' }); break
      case 'fill': await locator.fill(step.value); break
      case 'select': await locator.selectOption(step.value); break
      case 'click': await locator.click(); break
      case 'reload': await page.reload({ waitUntil: 'domcontentloaded' }); break
      case 'back': await page.goBack({ waitUntil: 'domcontentloaded' }); break
      case 'expect_text': await page.getByText(step.value, { exact: false }).first().waitFor(); break
      case 'expect_visible': await locator.waitFor({ state: 'visible' }); break
      case 'expect_path': await page.waitForURL(url => url.pathname === step.path); break
      case 'expect_count': {
        const deadline = Date.now() + 15000
        let actual = await locator.count()
        while (actual !== step.count && Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 100))
          actual = await locator.count()
        }
        if (actual !== step.count) throw new Error(`expected count ${step.count}, actual ${actual}`)
        break
      }
      default: throw new Error('script: unknown browser operation')
    }
    observations.push({ step: index + 1, kind: 'browser', action: step.action, selector: step.selector,
      expected: step.value ?? step.count ?? step.path, url: page.url(), assertion_passed: true })
  }
} catch (error) {
  evidence = String(error) + '\nBrowser diagnostics: ' + JSON.stringify(errors)
  failureKind = !activeAction ? 'environment'
    : !activeAction.startsWith('expect_') || /script:|Unexpected token|selector.*(parse|invalid)|strict mode violation/i.test(evidence) ? 'script' : 'product'
  status = failureKind === 'product' ? 'failed' : 'blocked'
} finally {
  await browser?.close()
}
process.stdout.write(JSON.stringify({ status, failure_kind: failureKind, evidence, observations }))
