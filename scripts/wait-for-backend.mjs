import { setTimeout } from 'node:timers/promises'

// Root `pnpm dev` starts both packages concurrently. Do not announce a usable
// frontend until the backend has finished migrations and application startup.
const deadline = Date.now() + 60_000
const url = 'http://127.0.0.1:8000/openapi.json'
console.log('Waiting for ForgeAI backend on port 8000…')
let ready = false
while (Date.now() < deadline) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(1000) })
    const schema = response.ok ? await response.json() : null
    if (schema?.paths?.['/api/v1/auth/me']) {
      ready = true
      break
    }
  } catch {
    // Expected while migrations or startup are still running.
  }
  await setTimeout(500)
}
if (!ready) {
  console.error('Backend did not become ready in 60 seconds. Check the backend startup log.')
  process.exitCode = 1
}
