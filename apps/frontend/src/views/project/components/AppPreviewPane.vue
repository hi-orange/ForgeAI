<template>
  <section class="app-viewer" aria-label="App 预览">
    <header class="viewer-toolbar">
      <strong>App 预览</strong>
      <button
        type="button"
        class="design-toggle"
        :class="{ active: designOpen }"
        :disabled="preview?.status !== 'ready' || designLoading"
        title="打开 Design"
        @click="toggleDesign"
      >
        <WorkbenchIcon name="paintbrush" />
        <span>{{ designLoading ? '加载中…' : 'Design' }}</span>
      </button>
      <div class="device-switcher" aria-label="预览设备">
        <button
          v-for="option in devices"
          :key="option.id"
          type="button"
          :class="{ active: device === option.id }"
          :aria-pressed="device === option.id"
          :title="option.label"
          @click="device = option.id"
        >
          <WorkbenchIcon :name="option.icon" />
          <span>{{ option.label }}</span>
        </button>
      </div>
      <div class="viewer-address">
        <WorkbenchIcon name="home" />
        <select
          v-if="previewPages.length > 1"
          :value="currentPath"
          aria-label="切换预览页面"
          @change="navigatePreview(($event.target as HTMLSelectElement).value)"
        >
          <option v-for="page in previewPages" :key="page.path" :value="page.path">
            {{ page.label }} · {{ page.path }}
          </option>
        </select>
        <span v-else>{{ displayAddress }}</span>
      </div>
      <div class="viewer-actions">
        <button
          type="button"
          :disabled="refreshing || previewBusy"
          title="重新加载预览"
          @click="reload"
        >
          <WorkbenchIcon name="refresh" />
          <span>刷新</span>
        </button>
        <button
          type="button"
          :disabled="!preview?.url"
          :title="preview?.url ? '在新标签页打开预览' : '预览尚未就绪'"
          aria-label="在新标签页打开"
          @click="openExternal"
        >
          <WorkbenchIcon name="external-link" />
        </button>
        <button
          type="button"
          :class="{ active: consoleOpen }"
          :aria-expanded="consoleOpen"
          @click="consoleOpen = !consoleOpen"
        >
          <WorkbenchIcon name="console" />
          <span>Console</span>
        </button>
      </div>
    </header>

    <div class="viewer-body">
      <PreviewDesignPanel
        v-if="designOpen"
        :state="designState"
        :selection="designSelection"
        :dirty="designDirty"
        :saving="designSaving"
        @close="closeDesign"
        @save="saveDesign"
        @discard="discardDesign"
        @apply-theme="applyTheme"
        @update-theme="updateTheme"
        @update-selection="updateSelection"
      />
      <div class="viewer-main">
        <div class="viewer-stage">
          <div class="device-frame" :style="frameStyle" :data-device="device">
            <section v-if="status?.error" class="viewer-error" role="alert">
              <span class="error-badge">!</span>
              <h2>构建遇到问题</h2>
              <p>{{ status.error }}</p>
              <button v-if="canResume" type="button" :disabled="busy" @click="$emit('resolve')">
                {{ busy ? '正在处理…' : '修复问题' }}
              </button>
            </section>

            <div v-else-if="showPlanOverview" class="plan-overview">
              <span class="eyebrow">{{ canApprove ? '待批准的建议' : '当前计划' }}</span>
              <h1>{{ planGoal }}</h1>
              <ul>
                <li v-for="item in selectedItems" :key="item.id">
                  <WorkbenchIcon name="check" />{{ item.label }}
                </li>
              </ul>
              <p v-if="canApprove">在左侧勾选、编辑或新增需求，批准后继续。</p>
            </div>

            <div v-else-if="preview?.status === 'ready' && preview.url" class="live-preview">
              <iframe
                :key="iframeKey"
                ref="iframeRef"
                class="preview-frame"
                :src="preview.url"
                title="生成应用预览"
                referrerpolicy="no-referrer"
                @load="requestBridgeState"
              />
            </div>

            <div v-else class="preview-empty">
              <div class="preview-illustration" aria-hidden="true">
                <div class="mini-sidebar"><i /><i /><i /></div>
                <div class="mini-page">
                  <div class="mini-nav"><i /><i /></div>
                  <div class="mini-hero" />
                  <div class="mini-cards"><i /><i /><i /></div>
                </div>
                <span class="preview-spark">✦</span>
              </div>
              <span class="eyebrow">从想法到应用</span>
              <h1>{{ previewTitle }}</h1>
              <p>{{ previewDescription }}</p>
              <div class="progress-steps" aria-label="构建进度">
                <span class="done">描述想法</span><i />
                <span :class="{ done: planReady }">确认计划</span><i />
                <span :class="{ done: status?.code_ready }">生成应用</span><i />
                <span :class="{ done: status?.state === 'completed' }">运行验证</span>
              </div>
              <p v-if="preview?.status === 'starting'" class="preview-note verified">
                {{ preview.message || '正在启动本地预览（首次可能需要构建前端）…' }}
              </p>
              <p
                v-else-if="preview?.status === 'error'"
                class="preview-note preview-error"
                role="alert"
              >
                {{ preview.message || '预览启动失败' }}
              </p>
              <p v-else-if="preview?.status === 'disabled'" class="preview-note">
                本地预览未启用。可在后端配置 PREVIEW_ENABLED=true。
              </p>
              <p v-else-if="status?.state === 'completed'" class="preview-note verified">
                独立验收已通过。点击下方按钮启动本机预览。
              </p>
              <p v-else-if="status?.code_ready" class="preview-note">
                源码已经生成，可在顶部“编辑器”中查看；正在进行独立验收和隔离运行验证。
              </p>
              <button
                v-if="status?.state === 'completed' && preview?.status !== 'starting'"
                type="button"
                class="preview-start"
                :disabled="previewBusy"
                @click="ensurePreview(true)"
              >
                {{
                  previewBusy ? '启动中…' : preview?.status === 'error' ? '重试预览' : '启动预览'
                }}
              </button>
            </div>
          </div>
        </div>

        <aside v-if="consoleOpen" class="viewer-console" aria-label="运行日志">
          <div class="console-heading">
            <strong>运行日志</strong>
            <span>{{ consoleRows.length }} 条</span>
          </div>
          <p v-if="!consoleRows.length" class="console-empty">
            开始构建后，文件操作、页面日志和错误会显示在这里。
          </p>
          <ol v-else>
            <li v-for="row in consoleRows" :key="row.id" :class="{ failed: !row.ok }">
              <span>{{ row.ok ? '●' : '×' }}</span>
              <strong>{{ row.label }}</strong>
              <code v-if="row.detail">{{ row.detail }}</code>
            </li>
          </ol>
        </aside>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as previewApi from '@/api/modules/preview'
import type {
  PreviewDesignState,
  PreviewElementOverride,
  PreviewPage,
  PreviewSelection,
  PreviewStatus,
  PreviewTheme,
} from '@/api/modules/preview'
import type { RequirementsStatus } from '@/api/modules/requirements'
import type { RequirementPlanItem } from '../useRequirements'
import PreviewDesignPanel from './PreviewDesignPanel.vue'
import WorkbenchIcon, { type WorkbenchIconName } from './WorkbenchIcon.vue'

type PreviewDevice = 'desktop' | 'tablet' | 'mobile'

const props = defineProps<{
  projectId: number
  status: RequirementsStatus | null
  planGoal: string
  planItems: RequirementPlanItem[]
  canApprove: boolean
  canResume: boolean
  busy: boolean
  refreshing: boolean
}>()

const emit = defineEmits<{ refresh: []; resolve: [] }>()
const device = ref<PreviewDevice>('desktop')
const consoleOpen = ref(false)
const preview = ref<PreviewStatus | null>(null)
const previewBusy = ref(false)
const iframeKey = ref(0)
const iframeRef = ref<HTMLIFrameElement | null>(null)
const designOpen = ref(false)
const designLoading = ref(false)
const designSaving = ref(false)
const designDirty = ref(false)
const designLoadedKey = ref<string | null>(null)
const designSelection = ref<PreviewSelection | null>(null)
const previewPages = ref<PreviewPage[]>([])
const currentPath = ref('/')
const previewConsoleRows = ref<
  Array<{ id: string; level: string; message: string; source: 'page' | 'design' }>
>([])
let consoleSequence = 0
let activePreviewIdentity: string | null = null

function defaultDesignState(): PreviewDesignState {
  return {
    revision: 0,
    saved_at: null,
    theme: {
      preset_id: 'forge',
      name: 'Forge',
      background: '#f8fafc',
      surface: '#ffffff',
      text: '#0f172a',
      primary: '#2563eb',
      muted: '#64748b',
      border: '#e2e8f0',
      font_family: 'Inter, system-ui, sans-serif',
      radius: 12,
      shadow: '0 12px 32px rgba(15, 23, 42, 0.08)',
    },
    elements: [],
  }
}

const designState = ref<PreviewDesignState>(defaultDesignState())
const savedDesignState = ref<PreviewDesignState>(defaultDesignState())
let pollTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const devices: Array<{ id: PreviewDevice; label: string; icon: WorkbenchIconName }> = [
  { id: 'desktop', label: '桌面', icon: 'desktop' },
  { id: 'tablet', label: '平板', icon: 'tablet' },
  { id: 'mobile', label: '手机', icon: 'mobile' },
]

const frameStyle = computed(() => ({
  width: ({ desktop: '100%', tablet: '768px', mobile: '390px' } as const)[device.value],
}))
const displayAddress = computed(() => {
  if (!preview.value?.url) return 'Home'
  try {
    const url = new URL(preview.value.url)
    return `${url.origin}${currentPath.value}`
  } catch {
    return preview.value.url
  }
})
const selectedItems = computed(() => props.planItems.filter((item) => item.checked))
const showPlanOverview = computed(() => Boolean(props.status?.app_spec) && props.canApprove)
const postApproval = computed(() =>
  [
    'design_pending',
    'design_running',
    'engineering_pending',
    'engineering_running',
    'engineering_generated',
    'quality_pending',
    'quality_running',
    'completed',
    'quality_challenge',
    'quality_failed',
  ].includes(props.status?.state ?? ''),
)
const planReady = computed(
  () => props.canApprove || postApproval.value || props.status?.state === 'ready_for_delivery',
)
const previewTitle = computed(() => {
  if (preview.value?.status === 'starting') return '正在启动预览'
  if (preview.value?.status === 'error') return '预览启动失败'
  if (props.status?.state === 'completed') return '应用已完成运行验证'
  if (props.status?.state === 'quality_failed') return '质量验证未通过'
  if (props.status?.code_ready) return '应用代码已生成'
  if (postApproval.value) return '正在构建你的应用'
  if (props.canApprove) return '你的应用，即将从这里开始'
  return '把想法变成看得见的应用'
})
const previewDescription = computed(() => {
  if (preview.value?.status === 'starting')
    return '正在构建前端产物并启动本机前后端，完成后会直接显示在这里。'
  if (preview.value?.status === 'error') return '可以重试启动预览；源码仍可在「编辑器」中查看。'
  if (props.status?.state === 'completed')
    return '准确代码版本已经通过独立验收。启动本机预览后，可在此直接操作应用。'
  if (props.status?.state === 'quality_failed')
    return props.canResume
      ? '代码已保留。验收基础设施问题可以继续处理，不会重新生成业务代码。'
      : '代码已保留，但当前版本未通过独立验收。请查看左侧失败证据。'
  if (props.status?.code_ready) return '工作区已有真实代码，正在等待验证与安全预览运行时。'
  if (props.status?.state === 'engineering_running')
    return '工程任务正在读写工作区，进度会同步到 Console。'
  if (props.status?.state === 'design_running')
    return 'Architect 正在生成系统设计，完成后才会把准确设计交给 Code Engineer。'
  if (props.canApprove) return '在左侧确认功能清单，批准后开始生成应用。'
  return '描述你的想法，确认核心功能，应用生成后将在这里预览。'
})
const consoleRows = computed(() => {
  const rows = (props.status?.activities ?? []).map((activity) => ({
    id: activity.id,
    label: activity.label || activity.name,
    detail: activity.detail,
    ok: activity.ok,
  }))
  if (props.status?.error) {
    rows.push({ id: 'current-error', label: '构建错误', detail: props.status.error, ok: false })
  }
  if (preview.value?.message) {
    rows.push({
      id: 'preview-status',
      label: '预览',
      detail: preview.value.message,
      ok: preview.value.status === 'ready' && !preview.value.message,
    })
  }
  for (const row of previewConsoleRows.value) {
    rows.push({
      id: row.id,
      label: `${row.source === 'design' ? 'Design' : '页面'} ${row.level}`,
      detail: row.message,
      ok: row.level !== 'error',
    })
  }
  return rows
})

function cloneDesign(state: PreviewDesignState) {
  return JSON.parse(JSON.stringify(state)) as PreviewDesignState
}

function postBridge(type: string, payload: Record<string, unknown> = {}) {
  const message = JSON.parse(
    JSON.stringify({ channel: 'forgeai-preview-design', type, ...payload }),
  ) as Record<string, unknown>
  iframeRef.value?.contentWindow?.postMessage(message, '*')
}

function requestBridgeState() {
  postBridge('apply-state', { state: designState.value })
  postBridge('design-mode', { enabled: designOpen.value })
  postBridge('request-state')
}

function handleBridgeMessage(event: MessageEvent) {
  if (event.source !== iframeRef.value?.contentWindow) return
  const message = event.data as Record<string, unknown> | null
  if (!message || message.channel !== 'forgeai-preview-design') return
  if (message.type === 'ready' || message.type === 'route') {
    if (typeof message.path === 'string') currentPath.value = message.path
    if (Array.isArray(message.pages)) previewPages.value = message.pages as PreviewPage[]
    if (message.type === 'ready') requestBridgeState()
    return
  }
  if (message.type === 'selection') {
    designSelection.value = (message.selection as PreviewSelection | null) ?? null
    return
  }
  if (message.type === 'console' && typeof message.message === 'string') {
    previewConsoleRows.value.push({
      id: `page-log-${++consoleSequence}`,
      level: typeof message.level === 'string' ? message.level : 'log',
      message: message.message,
      source: 'page',
    })
    if (previewConsoleRows.value.length > 100) previewConsoleRows.value.shift()
  }
}

async function loadDesign(force = false) {
  const key = `${props.projectId}:${props.status?.run_id ?? ''}`
  if (
    designLoading.value ||
    props.status?.state !== 'completed' ||
    (!force && designLoadedKey.value === key)
  )
    return
  designLoading.value = true
  try {
    const state = await previewApi.getPreviewDesign(props.projectId)
    designState.value = cloneDesign(state)
    savedDesignState.value = cloneDesign(state)
    designDirty.value = false
    designLoadedKey.value = key
    postBridge('apply-state', { state })
  } catch (err) {
    previewConsoleRows.value.push({
      id: `page-log-${++consoleSequence}`,
      level: 'error',
      message: err instanceof Error ? err.message : '读取 Design 状态失败',
      source: 'design',
    })
  } finally {
    designLoading.value = false
  }
}

async function toggleDesign() {
  if (designOpen.value) {
    closeDesign()
    return
  }
  await loadDesign()
  designOpen.value = true
  postBridge('design-mode', { enabled: true })
}

function closeDesign() {
  if (designDirty.value && !window.confirm('Design 中有未保存的更改。要放弃并退出吗？')) return
  if (designDirty.value) discardDesign()
  designOpen.value = false
  designSelection.value = null
  postBridge('design-mode', { enabled: false })
}

function markDesignChanged() {
  designDirty.value = true
  postBridge('apply-state', { state: designState.value })
}

function applyTheme(theme: PreviewTheme) {
  designState.value.theme = JSON.parse(JSON.stringify(theme)) as PreviewTheme
  markDesignChanged()
}

function updateTheme(patch: Partial<PreviewTheme>) {
  designState.value.theme = { ...designState.value.theme, ...patch, preset_id: 'custom' }
  markDesignChanged()
}

function updateSelection(
  patch: Partial<Pick<PreviewElementOverride, 'styles' | 'text' | 'image_url'>>,
) {
  const selection = designSelection.value
  if (!selection) return
  let override = designState.value.elements.find((item) => item.selector === selection.selector)
  if (!override) {
    override = {
      selector: selection.selector,
      label: selection.label,
      styles: {},
      text: null,
      image_url: null,
    }
    designState.value.elements.push(override)
  }
  if (patch.styles) {
    override.styles = { ...override.styles, ...patch.styles }
    selection.styles = { ...selection.styles, ...patch.styles }
  }
  if ('text' in patch) {
    override.text = patch.text ?? null
    selection.text = patch.text ?? null
  }
  if ('image_url' in patch) {
    override.image_url = patch.image_url ?? null
    selection.image_url = patch.image_url ?? null
  }
  markDesignChanged()
}

async function saveDesign() {
  if (!designDirty.value || designSaving.value) return
  designSaving.value = true
  try {
    const saved = await previewApi.savePreviewDesign(props.projectId, {
      theme: designState.value.theme,
      elements: designState.value.elements,
    })
    designState.value = cloneDesign(saved)
    savedDesignState.value = cloneDesign(saved)
    designDirty.value = false
    postBridge('apply-state', { state: saved })
  } catch (err) {
    previewConsoleRows.value.push({
      id: `page-log-${++consoleSequence}`,
      level: 'error',
      message: err instanceof Error ? err.message : '保存 Design 失败',
      source: 'design',
    })
    consoleOpen.value = true
  } finally {
    designSaving.value = false
  }
}

function discardDesign() {
  designState.value = cloneDesign(savedDesignState.value)
  designDirty.value = false
  postBridge('apply-state', { state: designState.value })
}

function navigatePreview(path: string) {
  if (!path || path === currentPath.value) return
  postBridge('navigate', { path })
}

function clearPoll() {
  clearTimeout(pollTimer)
  pollTimer = undefined
}

function schedulePoll() {
  clearPoll()
  if (disposed || !['starting', 'ready'].includes(preview.value?.status ?? '')) return
  const delay = preview.value?.status === 'starting' ? 1500 : 3000
  pollTimer = setTimeout(() => {
    void refreshPreview()
  }, delay)
}

async function refreshPreview() {
  if (disposed || !Number.isFinite(props.projectId)) return
  try {
    preview.value = await previewApi.getPreview(props.projectId)
  } catch (err) {
    if (!disposed) {
      preview.value = {
        status: 'error',
        url: null,
        run_id: null,
        message: err instanceof Error ? err.message : '读取预览状态失败',
      }
    }
    return
  }
  if (['starting', 'ready'].includes(preview.value.status)) {
    schedulePoll()
    if (preview.value.status === 'ready') void loadDesign()
  } else {
    clearPoll()
  }
}

async function ensurePreview(force = false) {
  if (disposed || props.status?.state !== 'completed') return
  if (!force && (preview.value?.status === 'ready' || preview.value?.status === 'starting')) {
    if (['starting', 'ready'].includes(preview.value.status)) schedulePoll()
    return
  }
  previewBusy.value = true
  try {
    preview.value = await previewApi.startPreview(props.projectId)
    if (['starting', 'ready'].includes(preview.value.status)) schedulePoll()
    if (preview.value.status === 'ready') iframeKey.value += 1
    if (preview.value.status === 'ready') await loadDesign()
  } catch (err) {
    preview.value = {
      status: 'error',
      url: null,
      run_id: null,
      message: err instanceof Error ? err.message : '启动预览失败',
    }
  } finally {
    previewBusy.value = false
  }
}

function openExternal() {
  if (!preview.value?.url) return
  window.open(preview.value.url, '_blank', 'noopener,noreferrer')
}

async function reload() {
  emit('refresh')
  if (props.status?.state === 'completed') {
    if (preview.value?.status === 'ready') {
      iframeKey.value += 1
      await refreshPreview()
    } else {
      await ensurePreview(true)
    }
  }
}

watch(
  () => [props.projectId, props.status?.run_id, props.status?.state] as const,
  ([projectId, runId, state]) => {
    const identity = `${projectId}:${runId ?? ''}`
    if (activePreviewIdentity !== identity) {
      activePreviewIdentity = identity
      clearPoll()
      preview.value = null
      designOpen.value = false
      designDirty.value = false
      designLoadedKey.value = null
      designSelection.value = null
      designState.value = defaultDesignState()
      savedDesignState.value = defaultDesignState()
      previewPages.value = []
      previewConsoleRows.value = []
      currentPath.value = '/'
    }
    if (state === 'completed') void ensurePreview(false)
    else {
      clearPoll()
      preview.value = null
      designOpen.value = false
      designDirty.value = false
      designLoadedKey.value = null
      designSelection.value = null
      previewPages.value = []
      currentPath.value = '/'
    }
  },
  { immediate: true },
)

onMounted(() => window.addEventListener('message', handleBridgeMessage))

onBeforeUnmount(() => {
  disposed = true
  clearPoll()
  window.removeEventListener('message', handleBridgeMessage)
})
</script>

<style scoped lang="scss">
.app-viewer {
  position: relative;
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
  background: #f5f5f7;
}
.viewer-body,
.viewer-main {
  display: flex;
  flex: 1;
  min-width: 0;
  min-height: 0;
}
.viewer-main {
  flex-direction: column;
}
.viewer-toolbar {
  display: grid;
  grid-template-columns: auto auto auto minmax(120px, 1fr) auto;
  align-items: center;
  gap: 12px;
  min-height: 42px;
  padding: 0 12px;
  border-bottom: 1px solid #e8e8ec;
  background: #fff;
  color: #5c5e69;
  font-size: 11px;
  > strong {
    color: #27272a;
    white-space: nowrap;
  }
  button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    min-height: 28px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: #62636d;
    cursor: pointer;
  }
  button:hover:not(:disabled),
  button.active {
    background: #f0f1f8;
    color: #3f46a8;
  }
  button:disabled {
    cursor: not-allowed;
    opacity: 0.38;
  }
}
.design-toggle {
  padding: 0 8px;
  border: 1px solid #e4e4e7 !important;
}
.device-switcher,
.viewer-actions {
  display: flex;
  align-items: center;
  gap: 2px;
}
.device-switcher button span {
  display: none;
}
.viewer-address {
  display: flex;
  align-items: center;
  justify-self: center;
  gap: 7px;
  width: min(360px, 100%);
  padding: 5px 12px;
  border: 1px solid #e6e6ea;
  border-radius: 999px;
  background: #fafafa;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  select {
    width: 100%;
    min-width: 0;
    border: 0;
    outline: 0;
    background: transparent;
    color: inherit;
    font: inherit;
  }
  span {
    overflow: hidden;
    text-overflow: ellipsis;
  }
}
.viewer-stage {
  display: flex;
  flex: 1;
  min-height: 0;
  justify-content: center;
  overflow: auto;
  padding: 16px;
  scrollbar-color: transparent transparent;
  scrollbar-width: thin;
  &:hover {
    scrollbar-color: rgba(113, 113, 122, 0.36) transparent;
  }
  &::-webkit-scrollbar {
    width: 6px;
    height: 6px;
  }
  &::-webkit-scrollbar-thumb {
    border-radius: 999px;
    background: transparent;
  }
  &:hover::-webkit-scrollbar-thumb {
    background: rgba(113, 113, 122, 0.36);
  }
}
.device-frame {
  display: flex;
  min-height: 100%;
  max-width: 100%;
  overflow: hidden;
  flex-direction: column;
  border: 1px solid #e2e2e8;
  border-radius: 10px;
  background: #fff;
  box-shadow: 0 12px 32px rgba(31, 35, 48, 0.06);
  transition: width 180ms ease;
}
.live-preview {
  display: flex;
  flex: 1;
  min-height: 0;
  background: #fff;
}
.preview-frame {
  width: 100%;
  height: 100%;
  min-height: 480px;
  border: 0;
  background: #fff;
}
.preview-empty,
.viewer-error {
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  padding: 30px;
  text-align: center;
}
.preview-empty h1,
.viewer-error h2 {
  margin: 12px 0;
  font-size: clamp(20px, 2vw, 28px);
  font-weight: 550;
  letter-spacing: -0.6px;
}
.preview-empty > p,
.viewer-error p {
  max-width: 430px;
  margin: 0;
  color: #858794;
  line-height: 1.8;
}
.viewer-error button,
.preview-start {
  margin-top: 18px;
  border: 0;
  border-radius: 8px;
  background: #5b5bd6;
  color: #fff;
  padding: 9px 15px;
  cursor: pointer;
}
.preview-start:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
.error-badge {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 50%;
  background: #fee2e2;
  color: #b91c1c;
  font-size: 22px;
  font-weight: 700;
}
.eyebrow {
  color: #8b8fbd;
  font-size: 11px;
  letter-spacing: 2px;
}
.preview-illustration {
  position: relative;
  display: flex;
  width: 246px;
  height: 150px;
  gap: 9px;
  margin-bottom: 34px;
  padding: 12px;
  border: 1px solid #e4e6f5;
  border-radius: 12px;
  background: #f9faff;
  box-shadow: 0 20px 60px #536eff10;
  transform: rotate(-3deg);
}
.mini-sidebar {
  width: 54px;
  padding: 12px 5px;
  border-radius: 7px;
  background: #eff0fb;
  i {
    display: block;
    height: 4px;
    margin: 7px 2px;
    border-radius: 4px;
    background: #d6dcf5;
  }
}
.mini-page {
  flex: 1;
  padding: 8px;
  border-radius: 7px;
  background: #fff;
}
.mini-nav {
  display: flex;
  justify-content: space-between;
  i {
    width: 23px;
    height: 4px;
    border-radius: 4px;
    background: #e3e7f5;
  }
}
.mini-hero {
  height: 57px;
  margin-top: 15px;
  border-radius: 6px;
  background: linear-gradient(120deg, #e7e9ff, #e4f0ff);
}
.mini-cards {
  display: flex;
  gap: 6px;
  margin-top: 10px;
  i {
    flex: 1;
    height: 28px;
    border: 1px solid #edeff8;
    border-radius: 5px;
  }
}
.preview-spark {
  position: absolute;
  top: -18px;
  right: -14px;
  color: #9ba8ff;
  font-size: 36px;
}
.progress-steps {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 30px;
  color: #c3c4cf;
  font-size: 11px;
  i {
    width: 25px;
    height: 1px;
    background: #e5e6ed;
  }
  .done {
    color: #6366a8;
  }
}
.preview-note {
  margin-top: 16px !important;
  color: #8a6d3b !important;
  font-size: 12px;
}
.preview-note.verified {
  color: #417a50 !important;
}
.preview-note.preview-error {
  color: #b91c1c !important;
}
.plan-overview {
  flex: 1;
  overflow: auto;
  padding: clamp(24px, 5vw, 80px);
  h1 {
    font-size: 25px;
    line-height: 1.5;
  }
  ul {
    padding: 0;
    list-style: none;
  }
  li {
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 15px 0;
    border-bottom: 1px solid #eee;
    line-height: 1.8;
  }
  p {
    color: #8a8d9e;
  }
}
.viewer-console {
  max-height: 210px;
  overflow: auto;
  border-top: 1px solid #dedee5;
  background: #17181d;
  color: #d7d8df;
  font-size: 11px;
  padding: 12px 14px;
}
.console-heading {
  display: flex;
  justify-content: space-between;
  color: #f4f4f5;
}
.console-heading span,
.console-empty {
  color: #868894;
}
.viewer-console ol {
  display: grid;
  gap: 7px;
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
}
.viewer-console li {
  display: grid;
  grid-template-columns: 12px auto minmax(0, 1fr);
  gap: 8px;
  color: #9fe2b0;
  &.failed {
    color: #fca5a5;
  }
  strong {
    color: #e4e4e7;
  }
  code {
    overflow: hidden;
    color: #a1a1aa;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}
@media (max-width: 760px) {
  .viewer-toolbar {
    grid-template-columns: auto auto auto 1fr;
  }
  .viewer-address {
    display: none;
  }
  .viewer-actions span {
    display: none;
  }
}
@media (max-width: 980px) {
  .design-panel {
    position: absolute;
    inset: 42px auto 0 0;
    z-index: 20;
    box-shadow: 14px 0 40px rgba(15, 23, 42, 0.14);
  }
}
</style>
