import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { test, mock } from 'node:test'
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm'
import ts from 'typescript'
import { computed, ref } from 'vue'

// 使用真实 Vue 响应式和生产 composable，仅替换生命周期、计时器和 HTTP 边界。
// 不需要浏览器，不安装新的测试依赖；这里不检验页面视觉布局。
async function loadModule(path, imports, globals = {}) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8')
  const code = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  }).outputText
  const context = createContext({ Error, crypto: { randomUUID }, ...globals })
  const module = new SourceTextModule(code, { context })
  await module.link((name) => {
    assert.ok(imports[name], `Unexpected import: ${name}`)
    const values = imports[name]
    return new SyntheticModule(
      Object.keys(values),
      function () {
        for (const [key, value] of Object.entries(values)) this.setExport(key, value)
      },
      { context },
    )
  })
  await module.evaluate()
  return module.namespace
}

function progress(state = 'not_started', overrides = {}) {
  return {
    project_id: 7,
    run_id: 'run-1',
    plan_id: null,
    task_id: null,
    message_id: 3,
    state,
    execution_id: null,
    execution_expires_at: null,
    error: null,
    app_spec: null,
    result:
      state === 'needs_user_input'
        ? { configuration_item_id: 'ci-1', open_questions: ['导出格式？'] }
        : null,
    ...overrides,
  }
}

function deferred() {
  let resolve
  const promise = new Promise((done) => {
    resolve = done
  })
  return { promise, resolve }
}

function approvalState() {
  return progress('awaiting_approval', {
    result: { configuration_item_id: 'ci-proposal', open_questions: [] },
    app_spec: {
      goal: '查看记录', target_users: ['访客'],
      features: [{ id: 'feat_read', text: '登录后查看记录' }],
      data_requirements: [], interface_requirements: [], constraints: [], open_questions: [],
      acceptance_criteria: [{ id: 'ac_read', text: '未登录时拒绝访问记录', source_ids: ['feat_read'] }],
    },
  })
}

test('unchanged approved scope keeps existing acceptance without extra input', async () => {
  const f = await fixture(approvalState())
  await f.mount()
  assert.equal(f.view.needsAcceptance(f.view.planItems.value[0]), false)
  await f.view.approve()
  const selected = f.api.approveRequirements.mock.calls[0].arguments[4].selected
  assert.equal(selected[0].id, 'feat_read')
  assert.equal('acceptance' in selected[0], false)
  f.unmount()
})

test('editing a feature requires current acceptance and preserves retry identity', async () => {
  const f = await fixture(approvalState())
  await f.mount()
  const item = f.view.planItems.value[0]
  item.label = '匿名查看记录'
  assert.equal(f.view.needsAcceptance(item), true)
  await f.view.approve()
  assert.equal(f.api.approveRequirements.mock.callCount(), 0)
  assert.match(f.view.error.value, /怎样才算完成/)
  item.acceptance = '无需登录即可看到记录列表'
  f.api.approveRequirements.mock.mockImplementation(async () => { throw new Error('network failed') })
  await f.view.approve()
  await f.view.approve()
  const first = f.api.approveRequirements.mock.calls[0].arguments[4]
  const retry = f.api.approveRequirements.mock.calls[1].arguments[4]
  assert.equal(first.selected[0].text, '匿名查看记录')
  assert.equal(first.selected[0].acceptance, item.acceptance)
  assert.equal(first.client_message_id, retry.client_message_id)
  item.acceptance = '首页直接展示记录列表'
  await f.view.approve()
  const changed = f.api.approveRequirements.mock.calls[2].arguments[4]
  assert.notEqual(changed.client_message_id, first.client_message_id)
  f.unmount()
})

test('new feature requires acceptance but unchecked features do not block approval', async () => {
  const f = await fixture(approvalState())
  await f.mount()
  f.view.addPlanItem('搜索记录')
  const added = f.view.planItems.value[1]
  assert.equal(f.view.needsAcceptance(added), true)
  await f.view.approve()
  assert.equal(f.api.approveRequirements.mock.callCount(), 0)
  added.checked = false
  await f.view.approve()
  assert.equal(f.api.approveRequirements.mock.calls[0].arguments[4].selected.length, 1)
  f.unmount()
})

test('removing part of a joint criterion requests acceptance for the remaining feature', async () => {
  const state = approvalState()
  state.app_spec.features.push({ id: 'feat_export', text: '导出记录' })
  state.app_spec.acceptance_criteria[0].source_ids.push('feat_export')
  const f = await fixture(state)
  await f.mount()
  assert.equal(f.view.needsAcceptance(f.view.planItems.value[0]), false)
  f.view.planItems.value[1].checked = false
  assert.equal(f.view.needsAcceptance(f.view.planItems.value[0]), true)
  await f.view.approve()
  assert.equal(f.api.approveRequirements.mock.callCount(), 0)
  f.view.planItems.value[0].acceptance = '登录后显示记录列表'
  await f.view.approve()
  assert.equal(f.api.approveRequirements.mock.calls[0].arguments[4].selected.length, 1)
  f.unmount()
})

async function fixture(state = progress()) {
  const mounted = [],
    unmounted = [],
    timers = new Set()
  const api = {
    getRequirementsProject: mock.fn(async () => ({
      id: 7,
      name: '读书记录',
      prompt: '做一个读书记录应用',
    })),
    getRequirements: mock.fn(async () => structuredClone(state)),
    getRequirementMessages: mock.fn(async () => []),
    createRequirementMessage: mock.fn(async () => ({ id: 12, sequence: 12 })),
    classifyRequirementMessage: mock.fn(async () => ({ category: 'product_change' })),
    createRequirementsRun: mock.fn(async () => ({ run_id: 'run-new' })),
    executeRequirements: mock.fn(async () => {
      state = progress('ready_for_design')
    }),
    answerRequirements: mock.fn(async () => {
      state = progress('ready_for_design')
    }),
    approveRequirements: mock.fn(async () => {
      state = progress('design_pending')
    }),
  }
  const { useRequirements } = await loadModule(
    '../src/views/project/useRequirements.ts',
    {
      vue: {
        computed,
        ref,
        onMounted: (fn) => mounted.push(fn),
        onBeforeUnmount: (fn) => unmounted.push(fn),
      },
      '@/stores': { useAuthStore: () => ({ token: 'test-token' }) },
      '@/api/modules/requirements': api,
    },
    {
      setTimeout: (fn) => {
        timers.add(fn)
        return fn
      },
      clearTimeout: (fn) => timers.delete(fn),
    },
  )
  return {
    view: useRequirements(7),
    api,
    timers,
    mount: async () => {
      for (const fn of mounted) await fn()
    },
    unmount: () => {
      for (const fn of unmounted) fn()
    },
    setState: (value) => {
      state = value
    },
  }
}

test('mount submits the home prompt once and starts requirement planning', async () => {
  const f = await fixture()
  await f.mount()
  assert.equal(f.view.text.value, '')
  assert.equal(f.api.createRequirementMessage.mock.callCount(), 1)
  assert.equal(f.api.classifyRequirementMessage.mock.callCount(), 1)
  assert.equal(f.api.createRequirementsRun.mock.callCount(), 0)
  assert.equal(f.api.executeRequirements.mock.callCount(), 1)
  assert.equal(f.api.answerRequirements.mock.callCount(), 0)
  assert.equal(f.timers.size, 0)
  f.unmount()
})

test('mount executes the stored home message without posting a duplicate', async () => {
  const f = await fixture()
  f.api.getRequirementMessages.mock.mockImplementation(async () => [
    { id: 1, sequence: 1, sender: 'user', content: '做一个读书记录应用' },
  ])
  await f.mount()
  assert.equal(f.api.createRequirementMessage.mock.callCount(), 0)
  assert.equal(f.api.classifyRequirementMessage.mock.callCount(), 1)
  assert.equal(f.api.classifyRequirementMessage.mock.calls[0].arguments[2], 1)
  assert.equal(f.api.createRequirementsRun.mock.callCount(), 0)
  assert.equal(f.api.executeRequirements.mock.callCount(), 1)
  assert.deepEqual(Array.from(f.api.executeRequirements.mock.calls[0].arguments), [
    'test-token', 7, 'run-1', 1,
  ])
  f.unmount()
})

test('messages paginate and a refreshed waiting round binds the exact item', async () => {
  const f = await fixture(progress('needs_user_input'))
  f.api.getRequirementMessages.mock.mockImplementation(async (_token, _id, after) =>
    after === 0
      ? Array.from({ length: 200 }, (_, index) => ({ id: index + 1, sequence: index + 1 }))
      : [],
  )
  await f.mount()
  assert.equal(f.view.messages.value.length, 200)
  assert.equal(f.api.getRequirementMessages.mock.calls[1].arguments[2], 200)
  f.view.text.value = 'CSV'
  await f.view.submit()
  assert.deepEqual(Array.from(f.api.answerRequirements.mock.calls[0].arguments).slice(0, 5), [
    'test-token',
    7,
    'run-1',
    'ci-1',
    'CSV',
  ])
  assert.equal(f.api.classifyRequirementMessage.mock.callCount(), 0)
  assert.equal(f.view.status.value.state, 'ready_for_design')
  f.unmount()
})

test('double submit calls the answer endpoint only once', async () => {
  const f = await fixture(progress('needs_user_input'))
  const gate = deferred()
  f.api.answerRequirements.mock.mockImplementation(async () => {
    await gate.promise
    f.setState(progress('ready_for_design'))
  })
  await f.mount()
  f.view.text.value = 'CSV'
  const submission = f.view.submit()
  await f.view.submit()
  assert.equal(f.api.answerRequirements.mock.callCount(), 1)
  assert.equal(f.view.busy.value, true)
  gate.resolve()
  await submission
  assert.equal(f.view.busy.value, false)
  assert.equal(f.timers.size, 0)
  f.unmount()
})

test('retrying an unsaved answer preserves its idempotency key', async () => {
  const f = await fixture(progress('needs_user_input'))
  f.api.answerRequirements.mock.mockImplementationOnce(async () => {
    throw new Error('网络断开')
  })
  await f.mount()
  f.view.text.value = 'CSV'
  await f.view.submit()
  assert.equal(f.view.text.value, 'CSV')
  assert.equal(f.view.error.value, '网络断开')
  await f.view.submit()
  assert.equal(
    f.api.answerRequirements.mock.calls[0].arguments[5],
    f.api.answerRequirements.mock.calls[1].arguments[5],
  )
  f.unmount()
})

test('initial submission reuses a queued run and executes the saved message', async () => {
  const f = await fixture()
  await f.mount()
  await f.view.submit()
  assert.equal(f.api.createRequirementMessage.mock.callCount(), 1)
  assert.equal(f.api.classifyRequirementMessage.mock.calls[0].arguments[2], 12)
  assert.deepEqual(Array.from(f.api.executeRequirements.mock.calls[0].arguments), [
    'test-token',
    7,
    'run-1',
    12,
  ])
  assert.equal(f.api.createRequirementsRun.mock.callCount(), 0)
  f.unmount()
})

test('a question classified as inquiry does not create or execute a run', async () => {
  const f = await fixture(progress('not_started', { run_id: null }))
  f.api.classifyRequirementMessage.mock.mockImplementation(async () => ({ category: 'inquiry' }))
  await f.mount()
  await f.view.submit()
  assert.equal(f.api.createRequirementsRun.mock.callCount(), 0)
  assert.equal(f.api.executeRequirements.mock.callCount(), 0)
  assert.match(f.view.error.value, /描述你要做的应用/)
  f.unmount()
})

test('explicit recovery uses the current execution and pinned cause message', async () => {
  const f = await fixture(
    progress('retry_available', { execution_id: 'exec-failed', message_id: 28 }),
  )
  await f.mount()
  assert.equal(f.api.executeRequirements.mock.callCount(), 0)
  await f.view.resume()
  assert.deepEqual(Array.from(f.api.executeRequirements.mock.calls[0].arguments), [
    'test-token',
    7,
    'run-1',
    28,
    'exec-failed',
  ])
  assert.equal(f.api.createRequirementMessage.mock.callCount(), 0)
  f.unmount()
})

test('an older polling response cannot overwrite the final result', async () => {
  const f = await fixture(progress('needs_user_input'))
  await f.mount()
  const work = deferred(),
    reading = deferred()
  f.api.answerRequirements.mock.mockImplementation(async () => {
    await work.promise
    f.setState(progress('ready_for_design'))
  })
  f.view.text.value = 'CSV'
  const submission = f.view.submit()
  f.api.getRequirements.mock.mockImplementationOnce(() => reading.promise)
  const poll = f.view.refresh()
  work.resolve()
  reading.resolve(progress('running'))
  await Promise.all([submission, poll])
  assert.equal(f.view.status.value.state, 'ready_for_design')
  assert.equal(f.timers.size, 0)
  f.unmount()
})

test('unmount cancels polling and ignores late read results', async () => {
  const f = await fixture(progress('running'))
  await f.mount()
  assert.equal(f.timers.size, 1)
  const reading = deferred()
  f.api.getRequirements.mock.mockImplementationOnce(() => reading.promise)
  const poll = f.view.refresh()
  f.unmount()
  reading.resolve(progress('ready_for_design'))
  await poll
  assert.equal(f.view.status.value.state, 'running')
  assert.equal(f.timers.size, 0)
})

test('ready requirements resume the saved workflow without posting or classifying a new message', async () => {
  const f = await fixture(progress('ready_for_design', { message_id: 45 }))
  await f.mount()
  assert.equal(f.view.canResume.value, true)
  assert.equal(f.api.executeRequirements.mock.callCount(), 0)
  f.api.executeRequirements.mock.mockImplementation(async () => {
    f.setState(progress('design_pending'))
  })
  await f.view.resume()
  assert.deepEqual(Array.from(f.api.executeRequirements.mock.calls[0].arguments), [
    'test-token',
    7,
    'run-1',
    45,
    null,
  ])
  assert.equal(f.api.createRequirementMessage.mock.callCount(), 0)
  assert.equal(f.api.classifyRequirementMessage.mock.callCount(), 0)
  assert.equal(f.view.status.value.state, 'design_pending')
  assert.equal(f.view.canResume.value, false)
  f.unmount()
})

test('refreshing a design assignment preserves identifiers without auto-execution or polling', async () => {
  const f = await fixture(
    progress('design_pending', {
      result: {
        configuration_item_id: 'ci-pinned',
        design_plan_id: 'plan-design',
        design_task_id: 'task-design',
        open_questions: [],
      },
    }),
  )
  await f.mount()
  await f.view.refresh()
  assert.equal(f.view.status.value.result.configuration_item_id, 'ci-pinned')
  assert.equal(f.view.status.value.result.design_task_id, 'task-design')
  assert.equal(f.view.canWrite.value, false)
  assert.equal(f.view.canResume.value, false)
  await f.view.resume()
  assert.equal(f.api.executeRequirements.mock.callCount(), 0)
  assert.equal(f.timers.size, 0)
  f.unmount()
})

test('a failed dispatch retry keeps the continuation button available', async () => {
  const f = await fixture(progress('ready_for_design'))
  await f.mount()
  f.api.executeRequirements.mock.mockImplementation(async () => {
    throw new Error('派工失败')
  })
  await f.view.resume()
  assert.equal(f.view.canResume.value, true)
  assert.equal(f.view.status.value.state, 'ready_for_design')
  assert.equal(f.view.error.value, '派工失败')
  assert.equal(f.api.createRequirementMessage.mock.callCount(), 0)
  f.unmount()
})

test('HTTP wrappers carry authentication, exact identifiers, and answer keys', async () => {
  const request = mock.fn(async () => ({}))
  const api = await loadModule('../src/api/modules/requirements.ts', {
    '../request': { apiRequest: request },
  })
  await api.executeRequirements('token', 7, 'run-2', 33, 'exec-2')
  await api.answerRequirements('token', 7, 'run-2', 'ci-2', 'CSV', 'key-2')
  const calls = JSON.parse(JSON.stringify(request.mock.calls.map((call) => call.arguments)))
  assert.deepEqual(calls, [
    [
      '/api/v1/projects/7/build-runs/run-2/requirements',
      { method: 'POST', token: 'token', body: { message_id: 33, recovery_execution_id: 'exec-2' } },
    ],
    [
      '/api/v1/projects/7/build-runs/run-2/requirements/ci-2/answers',
      { method: 'POST', token: 'token', body: { content: 'CSV', client_message_id: 'key-2' } },
    ],
  ])
})
