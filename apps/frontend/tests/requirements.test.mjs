import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { test, mock } from 'node:test'
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm'
import ts from 'typescript'
import { computed, ref, reactive, watch, nextTick, effectScope } from 'vue'

test('presentation groups repeated model calls into one tool card and keeps stage narratives', async () => {
  const { buildGroups } = await loadModule('../src/views/project/buildTimeline.ts', {})
  const activities = []
  for (let i = 0; i < 12; i++) {
    activities.push({ id: 'model' + i, name: 'model', detail: '职位列表', ok: true })
    activities.push({
      id: 'read' + i,
      name: 'read_file',
      detail: 'backend/file' + i + '.py',
      ok: true,
    })
  }
  let groups = buildGroups(activities)
  assert.equal(groups.length, 1)
  assert.equal(groups[0].steps.length, 12)
  activities.push({
    id: 'summary',
    name: 'summary',
    detail: '接口已确认，现在编写列表页面',
    ok: true,
  })
  activities.push({ id: 'write', name: 'write_new_code', detail: 'frontend/src/App.vue', ok: true })
  groups = buildGroups(activities)
  assert.equal(groups.length, 2)
  assert.equal(groups[1].title, '接口已确认，现在编写列表页面')
  assert.equal(groups[1].steps[0].path, 'frontend/src/App.vue')
})

async function sourceFixture() {
  const input = reactive({
    projectId: 7,
    runId: 'run-1',
    ready: true,
    generation: 0,
    writtenPath: null,
  })
  const contents = { 'frontend/src/App.vue': 'template', 'backend/main.py': 'backend' }
  const api = {
    getWorkspace: mock.fn(async () => ({
      ready: true,
      run_id: input.runId,
      files: Object.keys(contents).map((path) => ({ path, size_bytes: 1 })),
    })),
    getWorkspaceFile: mock.fn(async (_id, path) => ({
      run_id: input.runId,
      path,
      content: contents[path],
    })),
  }
  const stops = []
  const module = await loadModule('../src/views/project/useWorkspaceSource.ts', {
    vue: { ref, watch, onBeforeUnmount: (fn) => stops.push(fn) },
    '@/api/modules/workspace': api,
    '@/stores': { useAuthStore: () => ({ token: 'test-token' }) },
  })
  const scope = effectScope()
  const view = scope.run(() => module.useWorkspaceSource(input))
  const settle = async () => {
    await nextTick()
    await new Promise((resolve) => setImmediate(resolve))
  }
  await settle()
  return {
    input,
    contents,
    api,
    view,
    settle,
    stop() {
      stops.forEach((fn) => fn())
      scope.stop()
    },
  }
}

test('source viewer opens template and refreshes same file on every successful write', async () => {
  const f = await sourceFixture()
  assert.equal(f.view.fileContent.value, 'template')
  f.contents['frontend/src/App.vue'] = 'first write'
  f.input.writtenPath = 'frontend/src/App.vue'
  f.input.generation++
  await f.settle()
  assert.equal(f.view.fileContent.value, 'first write')
  f.contents['frontend/src/App.vue'] = 'second write'
  f.input.generation++
  await f.settle()
  assert.equal(f.view.fileContent.value, 'second write')
  f.stop()
})

test('source viewer preserves manual selection while updating and can follow new files', async () => {
  const f = await sourceFixture()
  await f.view.selectFile('backend/main.py')
  f.contents['frontend/src/Jobs.vue'] = 'new page'
  f.input.writtenPath = 'frontend/src/Jobs.vue'
  f.input.generation++
  await f.settle()
  assert.equal(f.view.selectedPath.value, 'backend/main.py')
  assert.equal(f.view.fileContent.value, 'backend')
  f.view.followWrites()
  await f.settle()
  assert.equal(f.view.selectedPath.value, 'frontend/src/Jobs.vue')
  assert.equal(f.view.fileContent.value, 'new page')
  f.stop()
})

test('source viewer rejects late file responses after selection or run changes', async () => {
  const f = await sourceFixture()
  const late = deferred()
  f.api.getWorkspaceFile.mock.mockImplementationOnce(() => late.promise)
  const pending = f.view.selectFile('frontend/src/App.vue')
  await f.view.selectFile('backend/main.py')
  late.resolve({ run_id: 'run-1', content: 'stale content' })
  await pending
  assert.equal(f.view.fileContent.value, 'backend')
  f.input.runId = 'run-2'
  f.contents['frontend/src/App.vue'] = 'new run'
  await f.settle()
  assert.equal(f.view.fileContent.value, 'new run')
  f.stop()
})

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
      goal: '查看记录',
      target_users: ['访客'],
      features: [{ id: 'feat_read', text: '登录后查看记录' }],
      data_requirements: [],
      interface_requirements: [],
      constraints: [],
      open_questions: [],
      acceptance_criteria: [
        { id: 'ac_read', text: '未登录时拒绝访问记录', source_ids: ['feat_read'] },
      ],
    },
  })
}

test('unchanged approved scope keeps existing acceptance without extra input', async () => {
  const f = await fixture(approvalState())
  await f.mount()
  assert.equal(f.view.needsAcceptance(f.view.planItems.value[0]), false)
  await f.view.approve()
  const selected = f.api.approveRequirements.mock.calls[0].arguments[3].selected
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
  f.api.approveRequirements.mock.mockImplementation(async () => {
    throw new Error('network failed')
  })
  await f.view.approve()
  await f.view.approve()
  const first = f.api.approveRequirements.mock.calls[0].arguments[3]
  const retry = f.api.approveRequirements.mock.calls[1].arguments[3]
  assert.equal(first.selected[0].text, '匿名查看记录')
  assert.equal(first.selected[0].acceptance, item.acceptance)
  assert.equal(first.client_message_id, retry.client_message_id)
  item.acceptance = '首页直接展示记录列表'
  await f.view.approve()
  const changed = f.api.approveRequirements.mock.calls[2].arguments[3]
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
  assert.equal(f.api.approveRequirements.mock.calls[0].arguments[3].selected.length, 1)
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
  assert.equal(f.api.approveRequirements.mock.calls[0].arguments[3].selected.length, 1)
  f.unmount()
})

async function fixture(state = progress()) {
  const mounted = [],
    unmounted = [],
    timers = new Set()
  const messages = []
  const api = {
    getRequirementsProject: mock.fn(async () => ({
      id: 7,
      name: '读书记录',
      prompt: '做一个读书记录应用',
    })),
    getRequirements: mock.fn(async () => structuredClone(state)),
    getRequirementMessages: mock.fn(async (_id, after = 0) =>
      messages.filter((message) => message.sequence > after).slice(0, 200),
    ),
    submitRequirements: mock.fn(async (_id, content) => {
      const sequence = (messages.at(-1)?.sequence ?? 0) + 1
      messages.push({
        id: sequence,
        sequence,
        sender: 'user',
        content,
        client_message_id: `key-${sequence}`,
      })
      state = progress('ready_for_delivery', { message_id: sequence })
      return structuredClone(state)
    }),
    startRequirements: mock.fn(async () => {
      state = progress('ready_for_delivery', { message_id: messages[0]?.id ?? 1 })
      return structuredClone(state)
    }),
    continueRequirements: mock.fn(async () => {
      if (state.state === 'design_pending' || state.state === 'engineering_running') {
        state = progress('engineering_running', {
          activities: [{ id: 'start', name: 'start' }],
          result: state.result,
        })
      } else if (state.state === 'ready_for_delivery') {
        state = progress('design_pending', {
          message_id: state.message_id,
          result: {
            configuration_item_id: 'ci-1',
            open_questions: [],
          },
        })
      } else {
        state = progress('engineering_running', {
          activities: [{ id: 'start', name: 'start' }],
          result: state.result,
        })
      }
      return structuredClone(state)
    }),
    approveRequirements: mock.fn(async () => {
      state = progress('design_pending')
      return structuredClone(state)
    }),
    pauseBuildRun: mock.fn(async () => {
      state = progress('retry_available', {
        task_id: state.task_id,
        execution_id: state.execution_id,
        error: '用户已暂停，可继续处理。',
      })
      return structuredClone(state)
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
    messages,
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

test('mount starts requirement planning from the home prompt', async () => {
  const f = await fixture()
  await f.mount()
  assert.equal(f.view.text.value, '')
  assert.equal(f.api.startRequirements.mock.callCount(), 1)
  assert.equal(f.api.submitRequirements.mock.callCount(), 0)
  assert.equal(f.timers.size, 0)
  f.unmount()
})

test('mount starts from the stored home message without posting a duplicate', async () => {
  const f = await fixture()
  f.messages.push({
    id: 1,
    sequence: 1,
    sender: 'user',
    content: '做一个读书记录应用',
    client_message_id: 'project:7:initial',
  })
  await f.mount()
  assert.equal(f.api.startRequirements.mock.callCount(), 1)
  assert.equal(f.api.submitRequirements.mock.callCount(), 0)
  assert.deepEqual(Array.from(f.api.startRequirements.mock.calls[0].arguments), [7])
  f.unmount()
})

test('failed initial model connection can retry the stored message without posting a duplicate', async () => {
  const f = await fixture(progress('not_started', { run_id: null }))
  f.messages.push({
    id: 1,
    sequence: 1,
    sender: 'user',
    content: '做一个读书记录应用',
    client_message_id: 'project:7:initial',
  })
  f.api.startRequirements.mock.mockImplementationOnce(async () => {
    throw new Error('无法连接大模型服务，请检查网络或代理设置后重试')
  })
  await f.mount()
  assert.equal(f.view.canRetryStart.value, true)
  assert.equal(f.view.error.value, '无法连接大模型服务，请检查网络或代理设置后重试')
  await f.view.retryStart()
  assert.equal(f.api.startRequirements.mock.callCount(), 2)
  assert.equal(f.api.submitRequirements.mock.callCount(), 0)
  assert.equal(f.view.error.value, '')
  assert.equal(f.view.canRetryStart.value, false)
  f.unmount()
})

test('messages paginate and a refreshed waiting round binds the exact item', async () => {
  const f = await fixture(progress('needs_user_input'))
  for (let index = 0; index < 200; index++) {
    f.messages.push({ id: index + 1, sequence: index + 1, sender: 'user', content: 'm' })
  }
  await f.mount()
  assert.equal(f.view.messages.value.length, 200)
  assert.equal(f.api.getRequirementMessages.mock.calls[1].arguments[1], 200)
  f.view.text.value = 'CSV'
  await f.view.submit()
  assert.deepEqual(Array.from(f.api.submitRequirements.mock.calls[0].arguments).slice(0, 2), [
    7,
    'CSV',
  ])
  assert.equal(f.view.status.value.state, 'ready_for_delivery')
  f.unmount()
})

test('double submit calls the submit endpoint only once', async () => {
  const f = await fixture(progress('needs_user_input'))
  const gate = deferred()
  f.api.submitRequirements.mock.mockImplementation(async () => {
    await gate.promise
    f.setState(progress('ready_for_delivery'))
    return progress('ready_for_delivery')
  })
  await f.mount()
  f.view.text.value = 'CSV'
  const submission = f.view.submit()
  await f.view.submit()
  assert.equal(f.api.submitRequirements.mock.callCount(), 1)
  assert.equal(f.view.busy.value, true)
  gate.resolve()
  await submission
  assert.equal(f.view.busy.value, false)
  assert.equal(f.timers.size, 0)
  f.unmount()
})

test('retrying an unsaved answer preserves its idempotency key', async () => {
  const f = await fixture(progress('needs_user_input'))
  f.api.submitRequirements.mock.mockImplementationOnce(async () => {
    throw new Error('网络断开')
  })
  await f.mount()
  f.view.text.value = 'CSV'
  await f.view.submit()
  assert.equal(f.view.text.value, 'CSV')
  assert.equal(f.view.error.value, '网络断开')
  await f.view.submit()
  assert.equal(
    f.api.submitRequirements.mock.calls[0].arguments[2],
    f.api.submitRequirements.mock.calls[1].arguments[2],
  )
  f.unmount()
})

test('initial submission uses the coarse submit action', async () => {
  const f = await fixture(progress('not_started', { run_id: null }))
  f.api.getRequirementsProject.mock.mockImplementation(async () => ({
    id: 7,
    name: '读书记录',
    prompt: null,
  }))
  await f.mount()
  assert.equal(f.api.startRequirements.mock.callCount(), 0)
  f.view.text.value = '做一个读书记录应用'
  await f.view.submit()
  assert.equal(f.api.submitRequirements.mock.callCount(), 1)
  assert.deepEqual(Array.from(f.api.submitRequirements.mock.calls[0].arguments).slice(0, 2), [
    7,
    '做一个读书记录应用',
  ])
  f.unmount()
})

test('a question classified as inquiry does not create or execute a run', async () => {
  const f = await fixture(progress('not_started', { run_id: null }))
  f.api.startRequirements.mock.mockImplementation(async () => {
    f.messages.push({
      id: 2,
      sequence: 2,
      sender: 'assistant',
      content: '先具体描述一下你想做的应用或功能，我再帮你整理构建计划。',
      client_message_id: 'guidance:msg:1:inquiry',
    })
    return progress('not_started', { run_id: null })
  })
  f.messages.push({
    id: 1,
    sequence: 1,
    sender: 'user',
    content: '你好',
    client_message_id: 'k1',
  })
  await f.mount()
  assert.equal(f.api.startRequirements.mock.callCount(), 1)
  assert.equal(f.view.error.value, '')
  assert.match(f.view.messages.value.at(-1)?.content ?? '', /描述一下你想做的应用/)
  f.unmount()
})

test('explicit recovery uses continue', async () => {
  const f = await fixture(
    progress('retry_available', { execution_id: 'exec-failed', message_id: 28 }),
  )
  await f.mount()
  assert.equal(f.api.continueRequirements.mock.callCount(), 0)
  await f.view.resume()
  assert.deepEqual(Array.from(f.api.continueRequirements.mock.calls[0].arguments), [7])
  assert.equal(f.api.submitRequirements.mock.callCount(), 0)
  f.unmount()
})

test('an older polling response cannot overwrite the final result', async () => {
  const f = await fixture(progress('needs_user_input'))
  await f.mount()
  const work = deferred(),
    reading = deferred()
  f.api.submitRequirements.mock.mockImplementation(async () => {
    await work.promise
    f.setState(progress('ready_for_delivery'))
    return progress('ready_for_delivery')
  })
  f.view.text.value = 'CSV'
  const submission = f.view.submit()
  f.api.getRequirements.mock.mockImplementationOnce(() => reading.promise)
  const poll = f.view.refresh()
  work.resolve()
  reading.resolve(progress('running'))
  await Promise.all([submission, poll])
  assert.equal(f.view.status.value.state, 'ready_for_delivery')
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
  reading.resolve(progress('ready_for_delivery'))
  await poll
  assert.equal(f.view.status.value.state, 'running')
  assert.equal(f.timers.size, 0)
})

test('ready requirements automatically dispatch and start without posting a new message', async () => {
  const f = await fixture(progress('ready_for_delivery', { message_id: 45 }))
  await f.mount()
  assert.equal(f.view.canResume.value, false)
  assert.equal(f.api.continueRequirements.mock.callCount(), 2)
  assert.equal(f.api.submitRequirements.mock.callCount(), 0)
  assert.equal(f.view.status.value.state, 'engineering_running')
  f.unmount()
})

test('mounting a pending design assignment automatically starts engineering', async () => {
  const f = await fixture(
    progress('design_pending', {
      task_id: 'task-design',
      result: {
        configuration_item_id: 'ci-pinned',
        open_questions: [],
      },
    }),
  )
  await f.mount()
  assert.equal(f.view.canWrite.value, false)
  assert.equal(f.api.continueRequirements.mock.callCount(), 1)
  assert.equal(f.view.status.value.state, 'engineering_running')
  assert.equal(f.view.canResume.value, false)
  assert.equal(f.timers.size, 1)
  f.unmount()
})

test('blocked engineering resumes the saved run without a new requirement message', async () => {
  const f = await fixture(progress('engineering_running', { error: '工程执行已暂停' }))
  await f.mount()
  assert.equal(f.timers.size, 0)
  assert.equal(f.view.canResume.value, true)
  await f.view.resume()
  assert.deepEqual(Array.from(f.api.continueRequirements.mock.calls[0].arguments), [7])
  assert.equal(f.api.submitRequirements.mock.callCount(), 0)
  assert.equal(f.view.status.value.state, 'engineering_running')
  f.unmount()
})

test('generated engineering stops polling and does not offer another completion', async () => {
  const f = await fixture(progress('engineering_generated'))
  await f.mount()
  assert.equal(f.timers.size, 0)
  assert.equal(f.view.canResume.value, false)
  assert.equal(f.view.canPause.value, false)
  f.unmount()
})

test('only an active execution can pause and the paused status replaces the running status', async () => {
  const f = await fixture(
    progress('engineering_running', {
      task_id: 'task-engineering',
      execution_id: 'exec-engineering',
      activities: [{ id: 'start', name: 'start' }],
    }),
  )
  await f.mount()
  assert.equal(f.view.canPause.value, true)
  assert.equal(f.timers.size, 1)
  await f.view.pause()
  assert.deepEqual(Array.from(f.api.pauseBuildRun.mock.calls[0].arguments), [7, 'run-1'])
  assert.equal(f.view.status.value.state, 'retry_available')
  assert.equal(f.view.canPause.value, false)
  assert.equal(f.view.canResume.value, true)
  assert.equal(f.timers.size, 0)
  f.unmount()
})

test('a running label without an active execution does not offer pause', async () => {
  const f = await fixture(progress('engineering_running'))
  await f.mount()
  assert.equal(f.view.canPause.value, false)
  await f.view.pause()
  assert.equal(f.api.pauseBuildRun.mock.callCount(), 0)
  f.unmount()
})

test('timeline merges starts and results in operation order without losing failure logs', async () => {
  const { timelineSteps } = await loadModule('../src/views/project/buildTimeline.ts', {})
  const rows = timelineSteps([
    { id: '1', operation_id: 'model', name: 'model', status: 'running', ok: true },
    { id: '2', operation_id: 'model', name: 'model', status: 'succeeded', ok: true },
    { id: '3', name: 'summary', detail: '现在检查数据库', ok: true },
    { id: '4', operation_id: 'check', name: 'run_check', status: 'running', ok: true },
    {
      id: '5',
      operation_id: 'check',
      name: 'run_check',
      status: 'failed',
      ok: false,
      output: 'missing id',
    },
    {
      id: '6',
      operation_id: 'write',
      name: 'edit_file_by_replace',
      status: 'running',
      ok: true,
      path: 'models.py',
    },
  ])
  assert.deepEqual(
    Array.from(rows, (row) => row.id),
    ['3', '5', '6'],
  )
  assert.equal(rows[1].output, 'missing id')
  assert.equal(rows[2].status, 'running')
  assert.equal(rows[2].path, 'models.py')
})

test('a failed dispatch retry keeps the continuation button available', async () => {
  const f = await fixture(progress('ready_for_delivery'))
  f.api.continueRequirements.mock.mockImplementation(async () => {
    throw new Error('派工失败')
  })
  await f.mount()
  assert.equal(f.view.canResume.value, true)
  assert.equal(f.view.status.value.state, 'ready_for_delivery')
  assert.equal(f.view.error.value, '派工失败')
  assert.equal(f.api.submitRequirements.mock.callCount(), 0)
  f.unmount()
})

test('HTTP wrappers post coarse actions without embedding tokens', async () => {
  const request = mock.fn(async () => ({}))
  const api = await loadModule('../src/api/modules/requirements.ts', {
    '../request': { apiRequest: request },
  })
  await api.submitRequirements(7, 'CSV', 'key-2')
  await api.startRequirements(7, 33)
  await api.continueRequirements(7)
  const calls = JSON.parse(JSON.stringify(request.mock.calls.map((call) => call.arguments)))
  assert.deepEqual(calls, [
    [
      '/api/v1/projects/7/requirements/submit',
      { method: 'POST', body: { content: 'CSV', client_message_id: 'key-2' } },
    ],
    ['/api/v1/projects/7/requirements/start', { method: 'POST', body: { message_id: 33 } }],
    ['/api/v1/projects/7/requirements/continue', { method: 'POST' }],
  ])
})
