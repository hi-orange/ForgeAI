import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useAuthStore } from '@/stores'
import * as api from '@/api/modules/requirements'

export function useRequirements(projectId: number) {
  const auth = useAuthStore()
  const name = ref('项目需求')
  const status = ref<api.RequirementsStatus | null>(null)
  const messages = ref<api.RequirementMessage[]>([])
  const text = ref('')
  const error = ref('')
  const busy = ref(false)
  const refreshing = ref(false)
  let disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let inFlight: Promise<void> | null = null
  let lastRequest: { content: string; itemId: string | null; key: string } | null = null

  const canWrite = computed(
    () => status.value?.state === 'not_started' || status.value?.state === 'needs_user_input',
  )
  const canResume = computed(
    () =>
      status.value?.state === 'pending' ||
      status.value?.state === 'retry_available' ||
      status.value?.state === 'ready_for_design',
  )

  function token() {
    if (!auth.token) throw new Error('请先登录')
    return auth.token
  }

  function schedule() {
    clearTimeout(timer)
    if (!disposed && (busy.value || status.value?.state === 'running')) {
      timer = setTimeout(() => void refresh(), 2500)
    }
  }

  async function load() {
    refreshing.value = true
    try {
      const current = await api.getRequirements(token(), projectId)
      if (disposed) return
      status.value = current
      // 消息只追加，分页读取可以恢复刷新前的对话，不把后来的消息作为任务输入。
      let page: api.RequirementMessage[]
      do {
        page = await api.getRequirementMessages(
          token(),
          projectId,
          messages.value.at(-1)?.sequence ?? 0,
        )
        if (disposed) return
        messages.value.push(...page)
      } while (page.length === 200)
    } catch (err) {
      if (!disposed) error.value = err instanceof Error ? err.message : '读取需求进度失败'
    } finally {
      refreshing.value = false
      schedule()
    }
  }

  function refresh(): Promise<void> {
    if (disposed) return Promise.resolve()
    if (!inFlight) {
      inFlight = load().finally(() => {
        inFlight = null
      })
    }
    return inFlight
  }

  async function perform(work: () => Promise<unknown>) {
    if (busy.value || disposed) return
    busy.value = true
    error.value = ''
    schedule()
    try {
      await work()
      if (!disposed) {
        text.value = ''
        lastRequest = null
      }
    } catch (err) {
      if (!disposed)
        error.value = err instanceof Error ? err.message : '需求处理失败，请刷新进度后重试'
    } finally {
      busy.value = false
      clearTimeout(timer)
      // 先等旧轮询结束，再重新读库，避免旧响应盖住刚刚完成的结果。
      if (inFlight) await inFlight
      await refresh()
    }
  }

  async function submit() {
    const content = text.value.trim()
    const current = status.value
    if (!content || !current || !canWrite.value) return
    const itemId = current.result?.configuration_item_id ?? null
    if (!lastRequest || lastRequest.content !== content || lastRequest.itemId !== itemId) {
      lastRequest = { content, itemId, key: crypto.randomUUID() }
    }
    const key = lastRequest.key
    await perform(async () => {
      if (current.state === 'needs_user_input' && current.run_id && itemId) {
        await api.answerRequirements(token(), projectId, current.run_id, itemId, content, key)
        return
      }
      const message = await api.createRequirementMessage(token(), projectId, content, key)
      const classification = await api.classifyRequirementMessage(token(), projectId, message.id)
      if (classification.category !== 'product_change') {
        throw new Error('请描述你要做的应用或功能，再开始整理需求')
      }
      const runId = current.run_id ?? (await api.createRequirementsRun(token(), projectId)).run_id
      await api.executeRequirements(token(), projectId, runId, message.id)
    })
  }

  async function resume() {
    const current = status.value
    if (!current?.run_id || !current.message_id || !canResume.value) return
    await perform(() =>
      api.executeRequirements(
        token(),
        projectId,
        current.run_id!,
        current.message_id!,
        current.state === 'retry_available' ? current.execution_id : null,
      ),
    )
  }

  onMounted(async () => {
    try {
      const project = await api.getRequirementsProject(token(), projectId)
      if (disposed) return
      name.value = project.name
      await refresh()
      if (!disposed && status.value?.state === 'not_started') text.value = project.prompt ?? ''
    } catch (err) {
      if (!disposed) error.value = err instanceof Error ? err.message : '读取项目失败'
    }
  })
  onBeforeUnmount(() => {
    disposed = true
    clearTimeout(timer)
  })

  return {
    name,
    status,
    messages,
    text,
    error,
    busy,
    refreshing,
    canWrite,
    canResume,
    refresh,
    submit,
    resume,
  }
}
