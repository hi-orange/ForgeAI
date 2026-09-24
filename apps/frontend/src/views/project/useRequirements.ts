import * as api from '@/api/modules/requirements'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

export type RequirementPlanItem = {
  id: string
  label: string
  checked: boolean
  kind: 'feature' | 'data' | 'interface' | 'constraint'
  acceptance?: string
}

export function useRequirements(projectId: number) {
  const name = ref('项目需求')
  const status = ref<api.RequirementsStatus | null>(null)
  const messages = ref<api.RequirementMessage[]>([])
  const text = ref('')
  const error = ref('')
  const busy = ref(false)
  const pausing = ref(false)
  const refreshing = ref(false)
  const planGoal = ref('')
  const planItems = ref<RequirementPlanItem[]>([])
  let draftItemId: string | null = null
  let lastApproval: { signature: string; key: string } | null = null
  let disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let inFlight: Promise<void> | null = null
  let lastRequest: { content: string; itemId: string | null; key: string } | null = null
  let mutationVersion = 0

  const canWrite = computed(() =>
    ['not_started', 'needs_user_input', 'awaiting_approval'].includes(status.value?.state ?? ''),
  )
  const canApprove = computed(
    () =>
      ['awaiting_approval', 'needs_user_input'].includes(status.value?.state ?? '') &&
      Boolean(status.value?.app_spec),
  )
  const selectedCount = computed(
    () =>
      planItems.value.filter((item) => item.kind === 'feature' && item.checked && item.label.trim())
        .length,
  )
  const checkedPlanCount = computed(
    () => planItems.value.filter((item) => item.checked && item.label.trim()).length,
  )

  function loadPlan(current: api.RequirementsStatus) {
    const itemId = current.result?.configuration_item_id ?? null
    if (itemId === draftItemId) return
    draftItemId = itemId
    lastApproval = null
    const spec = current.app_spec
    planGoal.value = spec?.goal ?? ''
    planItems.value = [
      ...(spec?.features ?? []).map((item): RequirementPlanItem => ({
        id: item.id,
        label: item.text,
        checked: true,
        kind: 'feature',
      })),
      ...(spec?.data_requirements ?? []).map((item): RequirementPlanItem => ({
        id: item.id,
        label: item.text,
        checked: true,
        kind: 'data',
      })),
      ...(spec?.interface_requirements ?? []).map((item): RequirementPlanItem => ({
        id: item.id,
        label: item.text,
        checked: true,
        kind: 'interface',
      })),
      ...(spec?.constraints ?? []).map((item): RequirementPlanItem => ({
        id: item.id,
        label: item.text,
        checked: true,
        kind: 'constraint',
      })),
    ]
  }

  function addPlanItem(label: string) {
    if (!canApprove.value || busy.value || !label.trim() || label.trim().length > 2000) return
    if (planItems.value.filter((item) => item.kind === 'feature').length >= 50) return
    planItems.value.push({
      id: `feat_${crypto.randomUUID().replaceAll('-', '').slice(0, 16)}`,
      label: label.trim(),
      checked: true,
      kind: 'feature',
    })
  }

  function needsAcceptance(item: RequirementPlanItem) {
    if (item.kind !== 'feature' || !item.checked) return false
    const spec = status.value?.app_spec
    const unchanged = new Set(
      planItems.value
        .filter(
          (entry) =>
            entry.kind === 'feature' &&
            entry.checked &&
            !entry.acceptance?.trim() &&
            spec?.features.some(
              (original) => original.id === entry.id && original.text === entry.label.trim(),
            ),
        )
        .map((entry) => entry.id),
    )
    return !spec?.acceptance_criteria.some(
      (criterion) =>
        criterion.source_ids.includes(item.id) &&
        criterion.source_ids.every((id) => unchanged.has(id)),
    )
  }

  const canResume = computed(
    () =>
      status.value?.state === 'pending' ||
      status.value?.state === 'retry_available' ||
      (status.value?.state === 'ready_for_delivery' && !busy.value) ||
      (['design_pending', 'engineering_pending', 'quality_pending'].includes(
        status.value?.state ?? '',
      ) &&
        Boolean(status.value?.error)) ||
      (status.value?.state === 'engineering_running' &&
        (Boolean(status.value.error) || !status.value.activities?.length)),
  )
  const canRetryStart = computed(
    () =>
      status.value?.state === 'not_started' &&
      Boolean(error.value) &&
      messages.value.some((message) => message.sender === 'user'),
  )
  const canPause = computed(
    () =>
      Boolean(status.value?.run_id && status.value.task_id && status.value.execution_id) &&
      (status.value?.state === 'running' ||
        status.value?.state === 'design_running' ||
        status.value?.state === 'engineering_running' ||
        status.value?.state === 'quality_running') &&
      !status.value.error,
  )

  function schedule() {
    clearTimeout(timer)
    if (disposed) return
    if (
      busy.value ||
      status.value?.state === 'running' ||
      status.value?.state === 'design_running' ||
      status.value?.state === 'design_pending' ||
      status.value?.state === 'engineering_pending' ||
      (status.value?.state === 'engineering_running' && !status.value.error) ||
      status.value?.state === 'quality_pending' ||
      status.value?.state === 'quality_running'
    ) {
      timer = setTimeout(
        () => void refresh(),
        status.value?.state === 'engineering_running' ? 1200 : 2500,
      )
      return
    }
  }

  async function load() {
    refreshing.value = true
    try {
      const current = await api.getRequirements(projectId)
      if (disposed) return
      status.value = current
      loadPlan(current)
      // 消息只追加，分页读取可以恢复刷新前的对话，不把后来的消息作为任务输入。
      let page: api.RequirementMessage[]
      do {
        page = await api.getRequirementMessages(projectId, messages.value.at(-1)?.sequence ?? 0)
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
    const version = mutationVersion
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
      // A pause fences the request that was already running. Its eventual conflict is expected
      // and must not replace the user-facing paused state with a stale error.
      if (!disposed && version === mutationVersion)
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
      status.value = await api.submitRequirements(projectId, content, key)
      loadPlan(status.value)
    })
  }

  async function startFromExistingMessage() {
    status.value = await api.startRequirements(projectId)
    loadPlan(status.value)
  }

  async function retryStart() {
    if (!canRetryStart.value) return
    await perform(startFromExistingMessage)
  }

  async function resume() {
    if (!status.value?.run_id || !canResume.value) return
    await perform(async () => {
      await continueOnce()
    })
  }

  async function continueOnce() {
    status.value = await api.continueRequirements(projectId)
    loadPlan(status.value)
  }

  async function pause() {
    const current = status.value
    if (!current?.run_id || !canPause.value || pausing.value || disposed) return
    mutationVersion += 1
    pausing.value = true
    error.value = ''
    try {
      status.value = await api.pauseBuildRun(projectId, current.run_id)
      loadPlan(status.value)
    } catch (err) {
      if (!disposed) error.value = err instanceof Error ? err.message : '暂停失败，请刷新进度后重试'
    } finally {
      pausing.value = false
      clearTimeout(timer)
      if (inFlight) await inFlight
      await refresh()
    }
  }

  async function approve() {
    const current = status.value
    const itemId = current?.result?.configuration_item_id
    if (!canApprove.value || !current?.run_id || !itemId || !selectedCount.value || busy.value)
      return
    const selected = planItems.value.filter((item) => item.checked)
    if (!planGoal.value.trim() || selected.some((item) => !item.label.trim())) {
      error.value = '请填写目标及勾选项的内容'
      return
    }
    if (selected.some((item) => needsAcceptance(item) && !item.acceptance?.trim())) {
      error.value = '请填写新增、修改或缺少验收条件的功能要怎样才算完成'
      return
    }
    const selection = {
      goal: planGoal.value.trim(),
      selected: selected.map((item) => ({
        id: item.id,
        text: item.label.trim(),
        kind: item.kind,
        ...(needsAcceptance(item) ? { acceptance: item.acceptance!.trim() } : {}),
      })),
    }
    const signature = JSON.stringify({ itemId, ...selection })
    if (lastApproval?.signature !== signature)
      lastApproval = { signature, key: crypto.randomUUID() }
    const payload = { ...selection, client_message_id: lastApproval.key }
    await perform(async () => {
      status.value = await api.approveRequirements(projectId, current.run_id!, itemId, payload)
      loadPlan(status.value)
      lastApproval = null
    })
  }

  onMounted(async () => {
    try {
      const project = await api.getRequirementsProject(projectId)
      if (disposed) return
      name.value = project.name
      await refresh()
      if (disposed) return
      if (status.value?.state !== 'not_started') return
      if (!messages.value.some((message) => message.sender === 'user') && !project.prompt?.trim()) {
        return
      }
      await perform(startFromExistingMessage)
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
    pausing,
    refreshing,
    planGoal,
    planItems,
    canApprove,
    selectedCount,
    checkedPlanCount,
    addPlanItem,
    needsAcceptance,
    approve,
    canWrite,
    canResume,
    canRetryStart,
    canPause,
    refresh,
    submit,
    resume,
    retryStart,
    pause,
  }
}
