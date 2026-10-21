// Exercise the shipped provider callbacks with controlled asynchronous API responses.
// Hook stubs keep refs alive for one mount; DOM rendering is covered by the template build.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require(process.argv[3])

class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

function mount() {
  const calls = []
  const pending = new Map()
  let value
  const apiRequest = async (path, init) => {
    calls.push({ path, init })
    if (pending.has(path)) return pending.get(path).promise
    if (path === '/auth/login') {
      const { username } = JSON.parse(init.body)
      return { user: { id: username, username }, access_token: `token-${username}` }
    }
    return { ok: true }
  }
  const react = {
    createContext: () => ({ Provider: 'provider' }),
    useCallback: (fn) => fn,
    useContext: () => value,
    useMemo: (fn) => fn(),
    useRef: (initial) => ({ current: initial }),
    useState: (initial) => [initial, () => {}],
  }
  const code = ts.transpileModule(fs.readFileSync(process.argv[2], 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const sandbox = {
    exports: {}, Headers, DOMException,
    require(name) {
      if (name === 'react') return react
      if (name === 'react/jsx-runtime') return { jsx: (_, props) => { value = props.value; return null } }
      if (name === './api') return { apiRequest, ApiError }
      throw new Error(`Unexpected import: ${name}`)
    },
  }
  vm.runInNewContext(code, sandbox, { filename: 'auth.js' })
  sandbox.exports.AuthProvider({ children: null })
  function defer(path) {
    let resolve, reject
    const promise = new Promise((yes, no) => { resolve = yes; reject = no })
    pending.set(path, { promise })
    return { resolve, reject, release: () => pending.delete(path) }
  }
  return { auth: value, calls, defer }
}

async function main() {
  for (const switchUser of [false, true]) {
    const { auth, defer, calls } = mount()
    await auth.login('alice', 'password')
    const delayed = defer('/private')
    const oldRequest = auth.request('/private')
    if (switchUser) await auth.login('bob', 'password')
    else await auth.logout()
    delayed.resolve({ private: 'alice-only' })
    await assert.rejects(oldRequest, { name: 'AbortError' })
    if (switchUser) {
      await auth.request('/new-private')
      assert.equal(calls.at(-1).init.headers.Authorization, 'Bearer token-bob')
    } else await assert.rejects(auth.request('/new-private'), { status: 401 })
  }
  {
    const { auth, defer, calls } = mount()
    const delayed = defer('/auth/login')
    const oldLogin = auth.login('alice', 'password')
    await auth.logout()
    delayed.resolve({ user: { id: 1, username: 'alice' }, access_token: 'late-token' })
    await oldLogin
    assert.equal(calls.at(-1).path, '/auth/logout')
    assert.equal(calls.at(-1).init.headers.Authorization, 'Bearer late-token')
    await assert.rejects(auth.request('/private'), { status: 401 })
  }
  {
    const { auth, defer, calls } = mount()
    await auth.login('alice', 'password')
    const delayed = defer('/private')
    const oldRequest = auth.request('/private')
    await auth.login('bob', 'password')
    delayed.reject(new ApiError('expired', 401))
    await assert.rejects(oldRequest, { status: 401 })
    await auth.request('/new-private')
    assert.equal(calls.at(-1).init.headers.Authorization, 'Bearer token-bob')
  }
  console.log('AUTH_CAPABILITY_FRONTEND_OK')
}

main().catch((error) => { console.error(error); process.exitCode = 1 })
