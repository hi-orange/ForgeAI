<template>
  <div
    class="workbench"
    :class="{
      'atoms-mode': generatedFiles,
      'design-mode': designMode,
      'chat-collapsed': chatCollapsed,
    }"
  >
    <ProjectTopbar
      v-if="generatedFiles"
      :name="project?.name"
      :status="project?.status"
      :workspace-view="workspaceView"
      :chat-collapsed="chatCollapsed"
      :history-open="historyOpen"
      @update:workspace-view="setWorkspaceView"
      @share="shareProject"
      @publish="publishProject"
      @toggle-chat="chatCollapsed = !chatCollapsed"
      @toggle-history="historyOpen = !historyOpen"
    />
    <aside class="chat-pane" :class="{ collapsed: chatCollapsed }">
      <header v-show="!designMode && !generatedFiles" class="chat-header">
        <RouterLink class="home-link" :to="{ name: 'home' }" title="返回首页">
          <ForgeLogo :size="22" />
        </RouterLink>
        <div class="chat-title-wrap">
          <p class="chat-eyebrow">工作台</p>
          <h1 class="chat-title">{{ project?.name || '新项目' }}</h1>
        </div>
      </header>

      <div v-show="!designMode" ref="threadRef" class="chat-thread">
        <p v-if="bootLoading" class="thread-hint">加载项目中…</p>
        <p v-else-if="bootError" class="thread-error">{{ bootError }}</p>

        <template v-else>
          <div v-if="project?.prompt && !generatedFiles" class="bubble user">
            <p>{{ project.prompt }}</p>
          </div>

          <div class="bubble agent">
            <div class="agent-meta">
              <span class="agent-avatar">PM</span>
              <div>
                <strong>Product Manager</strong>
                <p class="agent-step">{{ stepLabel }}</p>
              </div>
            </div>
            <p class="agent-text">{{ agentStatusText }}</p>
            <p v-if="workflowId && !generatedFiles" class="workflow-id">
              workflow: {{ workflowId }}
            </p>
          </div>

          <div v-if="showBuilderStatus" class="bubble agent builder-bubble">
            <div class="agent-meta">
              <span class="agent-avatar builder-avatar">WB</span>
              <div>
                <strong>Website Builder</strong>
                <p class="agent-step">{{ builderStepLabel }}</p>
              </div>
            </div>
            <p class="agent-text">{{ builderStatusText }}</p>
          </div>

          <div v-if="showPlanCard && !generatedFiles" class="plan-card">
            <p class="plan-intro">
              请从这些核心功能和页面设计中，选择您希望优先实现或进一步讨论的部分。
            </p>
            <ul class="plan-list">
              <li v-for="item in planItems" :key="item.id">
                <label>
                  <input
                    v-model="item.checked"
                    type="checkbox"
                    :disabled="planApproved || approving"
                  />
                  <span>{{ item.label }}</span>
                </label>
              </li>
            </ul>
            <div class="plan-actions">
              <button
                type="button"
                class="btn ghost"
                :disabled="planApproved || approving"
                @click="resetPlan"
              >
                调整计划
              </button>
              <button
                type="button"
                class="btn primary"
                :disabled="planApproved || approving || !selectedCount"
                @click="approvePlan"
              >
                {{ planApproved ? '已批准' : approving ? '保存中…' : '批准' }}
              </button>
            </div>
            <p v-if="approveError" class="plan-error">{{ approveError }}</p>
            <p v-if="planApproved" class="plan-done">
              规格已保存，后续 Website Builder 将基于所选区块继续。
            </p>
          </div>

          <div
            v-for="(item, index) in siteChatMessages"
            :key="`site-chat-${index}`"
            class="bubble"
            :class="item.role === 'user' ? 'user' : 'agent'"
          >
            <div v-if="item.role === 'assistant'" class="agent-meta">
              <span class="agent-avatar">AI</span>
              <div>
                <strong>Website Editor</strong>
                <p class="agent-step">整站修改</p>
              </div>
            </div>
            <p>{{ item.content }}</p>
          </div>
        </template>
      </div>

      <footer v-show="!designMode" class="chat-composer">
        <textarea
          v-model="followUp"
          rows="2"
          :placeholder="generatedFiles ? '描述要如何修改整个网站…' : '继续补充需求或修改计划…'"
          :disabled="!project || starting || revisingWebsite || building"
          @keydown.enter.exact.prevent="sendFollowUp"
        />
        <button
          type="button"
          class="send"
          title="发送"
          :disabled="!followUp.trim() || starting || revisingWebsite || building"
          @click="sendFollowUp"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 19V5" />
            <path d="m6 11 6-6 6 6" />
          </svg>
        </button>
      </footer>

      <VisualEditorPanel
        v-if="designMode"
        v-model:editor-tab="editorTab"
        v-model:element-instruction="elementInstruction"
        :selected-element="selectedElement"
        :editor-error="editorError"
        :can-undo="editHistory.length > 0"
        :suggesting="suggestingElement"
        :chat-messages="designChatMessages"
        :style-value="styleValue"
        :css-number="cssNumber"
        @undo="undoVisualEdit"
        @update-text="updateSelectedText"
        @update-style="updateStyle"
        @update-pixel="updatePixelStyle"
        @update-select="updateSelectStyle"
        @update-color="updateColorStyle"
        @ask-ai="askAiToChangeElement"
      />
    </aside>

    <section class="canvas-pane">
      <CanvasToolbar
        v-model:preview-mode="previewMode"
        v-model:active-page-id="activePageId"
        :generated="Boolean(generatedFiles)"
        :design-mode="designMode"
        :console-open="consoleOpen"
        :workspace-view="workspaceView"
        :status="project?.status"
        :status-label="statusLabel"
        :modes="previewModes"
        :pages="previewPages"
        @refresh="refreshPreview"
        @toggle-design="toggleDesignMode"
        @toggle-console="consoleOpen = !consoleOpen"
      />

      <div class="canvas-main">
        <WorkbenchWorkspacePanels
          v-if="generatedFiles && (workspaceView !== 'viewer' || historyOpen)"
          class="workspace-panels-host"
          :class="{ 'overlay-only': workspaceView === 'viewer' && historyOpen }"
          :workspace-view="workspaceView"
          :history-open="historyOpen"
          :files="generatedFiles!"
          :website-revision="project?.website_revision ?? 1"
          :status="project?.status"
          :product-name="websiteSpec?.product.name"
          :product-summary="websiteSpec?.product.summary"
          :updated-at="project?.updated_at"
          @close-history="historyOpen = false"
          @publish="publishProject"
          @download-file="downloadGeneratedFile"
          @download-all="downloadAllGeneratedFiles"
        />

        <div
          v-show="!generatedFiles || workspaceView === 'viewer'"
          class="canvas-body"
          :class="[previewMode, { 'website-mode': generatedFiles && canvasView === 'preview' }]"
        >
          <div v-if="building || project?.status === 'building'" class="canvas-empty">
            <div class="build-spinner" aria-hidden="true" />
            <p class="empty-title">Website Builder 正在构建网站…</p>
            <p class="empty-desc">正在生成页面结构、视觉样式和本地交互，请稍候。</p>
          </div>
          <div
            v-else-if="
              project?.status === 'build_failed' || project?.status === 'validation_failed'
            "
            class="canvas-empty"
          >
            <p class="empty-title">
              {{ project?.status === 'validation_failed' ? '网站未通过质量检查' : '网站构建失败' }}
            </p>
            <p class="empty-desc">{{ project.build_error || projects.error || '请稍后重试。' }}</p>
            <ul v-if="validationIssues.length" class="validation-issues">
              <li v-for="issue in validationIssues" :key="issue.description">
                {{ issue.description }}
              </li>
            </ul>
            <button type="button" class="btn primary" @click="runBuild">重新构建</button>
          </div>
          <div v-else-if="generatedFiles && canvasView === 'preview'" class="website-preview-shell">
            <iframe
              :key="previewKey"
              ref="websiteFrame"
              class="website-frame"
              title="生成的网站预览"
              sandbox="allow-scripts"
              :srcdoc="previewDocument"
              @load="reapplyPendingEdits"
            />
            <div
              v-if="designMode && hoveredElement && !selectedElement"
              class="canvas-hover-box"
              :style="hoverBoxStyle"
            >
              <span class="canvas-hover-label">
                {{ hoveredElement.tagName.toLowerCase() }}
              </span>
            </div>
            <div
              v-if="designMode && selectedElement"
              class="canvas-selection-box"
              :style="selectionBoxStyle"
            >
              <span class="canvas-selection-label">
                {{ selectionLabel }}
              </span>
            </div>
            <div
              v-if="designMode && selectedElement"
              class="canvas-element-prompt"
              :style="elementPromptStyle"
            >
              <span class="canvas-element-tag">
                T&nbsp; {{ selectedElement.tagName.toLowerCase() }}
              </span>
              <input
                v-model="elementInstruction"
                type="text"
                maxlength="2000"
                :disabled="suggestingElement"
                placeholder="告诉 AI 如何修改当前元素…"
                @keydown.enter.prevent="askAiToChangeElement"
              />
              <button
                type="button"
                :disabled="!elementInstruction.trim() || suggestingElement"
                title="修改当前元素"
                @click="askAiToChangeElement"
              >
                {{ suggestingElement ? '…' : '↑' }}
              </button>
            </div>
            <div v-if="designMode && editorError" class="canvas-editor-error">
              {{ editorError }}
            </div>
            <div v-if="designMode && hasPendingEdits" class="canvas-save-bar">
              <button
                type="button"
                class="btn ghost"
                :disabled="savingWebsite"
                @click="discardVisualEdits"
              >
                放弃
              </button>
              <button
                type="button"
                class="btn primary"
                :disabled="savingWebsite"
                @click="saveVisualEdits"
              >
                {{ savingWebsite ? '保存中…' : '保存' }}
              </button>
            </div>
          </div>
          <div v-else-if="starting || project?.status === 'running'" class="canvas-empty">
            <p class="empty-title">Product Manager 正在整理网站规格…</p>
            <p class="empty-desc">左侧可查看进度，完成后将展示页面结构与计划确认。</p>
          </div>
          <div v-else-if="project?.status === 'failed'" class="canvas-empty">
            <p class="empty-title">生成失败</p>
            <p class="empty-desc">
              {{ projects.error || '请返回首页重试，或在左侧继续补充需求。' }}
            </p>
            <button type="button" class="btn primary" @click="retryStart">重新生成</button>
          </div>
          <div v-else-if="websiteSpec" class="spec-panel">
            <div class="prd-head">
              <h2>{{ websiteSpec.product.name }}</h2>
              <p>{{ websiteSpec.product.summary }}</p>
            </div>

            <dl class="spec-summary">
              <div>
                <dt>目标用户</dt>
                <dd>{{ websiteSpec.product.target_audience }}</dd>
              </div>
              <div>
                <dt>核心目标</dt>
                <dd>{{ websiteSpec.product.primary_goal }}</dd>
              </div>
              <div>
                <dt>视觉方向</dt>
                <dd>{{ websiteSpec.design.style }} · {{ websiteSpec.design.tone }}</dd>
              </div>
            </dl>

            <section v-for="page in websiteSpec.site.pages" :key="page.id" class="spec-page">
              <div class="spec-page-head">
                <div>
                  <h3>{{ page.name }}</h3>
                  <p>{{ page.purpose }}</p>
                </div>
                <code>{{ page.path }}</code>
              </div>
              <div class="section-grid">
                <article v-for="section in page.sections" :key="section.id" class="section-card">
                  <span class="section-type">{{ section.type }}</span>
                  <h4>{{ section.title }}</h4>
                  <p>{{ section.description }}</p>
                  <ul v-if="section.content_points.length">
                    <li v-for="point in section.content_points" :key="point">{{ point }}</li>
                  </ul>
                </article>
              </div>
            </section>

            <section v-if="websiteSpec.requirements.features.length" class="spec-list">
              <h3>MVP 功能</h3>
              <ul>
                <li v-for="feature in websiteSpec.requirements.features" :key="feature">
                  {{ feature }}
                </li>
              </ul>
            </section>
          </div>
          <div v-else-if="project?.prd" class="prd-panel">
            <div class="prd-head">
              <h2>旧版产品需求文档</h2>
              <p>该项目使用旧格式，新生成的项目将展示结构化网站规格。</p>
            </div>
            <pre class="prd-content">{{ project.prd }}</pre>
          </div>
          <div v-else class="canvas-empty">
            <p class="empty-title">等待开始</p>
            <p class="empty-desc">提交需求后，这里会展示网站规格与后续预览。</p>
          </div>
        </div>

        <aside v-if="consoleOpen" class="workbench-console" aria-label="控制台">
          <header class="console-head">
            <strong>Console</strong>
            <button type="button" class="console-close" @click="consoleOpen = false">×</button>
          </header>
          <pre class="console-log">{{ consoleText }}</pre>
        </aside>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import type {
  EditableStyleName,
  GeneratedWebsiteFiles,
  ProjectElementAiHistoryItem,
  WebsiteElementPatch,
  WebsiteSpecification,
} from '@/api/modules/project'
import ForgeLogo from '@/components/ForgeLogo.vue'
import { useProjectStore } from '@/stores'
import ProjectTopbar from './components/ProjectTopbar.vue'
import CanvasToolbar from './components/CanvasToolbar.vue'
import VisualEditorPanel from './components/VisualEditorPanel.vue'
import WorkbenchWorkspacePanels from './components/WorkbenchWorkspacePanels.vue'
import { EDITOR_BRIDGE_SCRIPT } from './editorBridge'
import type {
  CanvasView,
  DesignChatMessage,
  EditorTab,
  ElementRect,
  PlanItem,
  PreviewMode,
  SelectedEditableElement,
  WorkspaceView,
} from './projectView'

const route = useRoute()
const projects = useProjectStore()

const bootLoading = ref(true)
const bootError = ref<string | null>(null)
const followUp = ref('')
const planApproved = ref(false)
const approveError = ref<string | null>(null)
const planItems = reactive<PlanItem[]>([])
const previewMode = ref<PreviewMode>('desktop')
const workspaceView = ref<WorkspaceView>('viewer')
const chatCollapsed = ref(false)
const historyOpen = ref(false)
const consoleOpen = ref(false)
const activePageId = ref('home')
const canvasView = ref<CanvasView>('spec')
const threadRef = ref<HTMLElement | null>(null)
const websiteFrame = ref<HTMLIFrameElement | null>(null)
const designMode = ref(false)
const editorTab = ref<EditorTab>('visual')
const selectedElement = ref<SelectedEditableElement | null>(null)
const hoveredElement = ref<Pick<SelectedEditableElement, 'elementId' | 'tagName' | 'rect'> | null>(
  null,
)
const pendingEdits = ref<Record<string, WebsiteElementPatch>>({})
const editHistory = ref<Record<string, WebsiteElementPatch>[]>([])
const lastHistoryWasTextFor = ref<string | null>(null)
const editorError = ref<string | null>(null)
const elementInstruction = ref('')
const designChatMessages = ref<DesignChatMessage[]>([])
const siteChatMessages = ref<ProjectElementAiHistoryItem[]>([])
const designAskPending = ref(false)
const previewKey = ref(0)
const starting = computed(() => projects.starting)
const approving = computed(() => projects.approving)
const building = computed(() => projects.building)
const savingWebsite = computed(() => projects.savingWebsite)
const suggestingElement = computed(
  () => projects.suggestingElement || projects.revisingWebsite || designAskPending.value,
)
const revisingWebsite = computed(() => projects.revisingWebsite)
const hasPendingEdits = computed(() => Object.keys(pendingEdits.value).length > 0)

watch(
  () => selectedElement.value?.elementId ?? null,
  (nextId, prevId) => {
    if (nextId && nextId !== prevId) {
      designChatMessages.value = []
      elementInstruction.value = ''
      editorError.value = null
    }
  },
)

const project = computed(() => projects.current)
const workflowId = computed(() => projects.workflowId)

const websiteSpec = computed<WebsiteSpecification | null>(() => {
  const rawSpec = project.value?.approved_spec || project.value?.prd
  if (!rawSpec) return null
  try {
    const parsed: unknown = JSON.parse(rawSpec)
    if (!parsed || typeof parsed !== 'object') return null
    const candidate = parsed as Partial<WebsiteSpecification>
    if (!candidate.product || !candidate.site || !Array.isArray(candidate.site.pages)) return null
    return candidate as WebsiteSpecification
  } catch {
    return null
  }
})

const generatedFiles = computed<GeneratedWebsiteFiles | null>(() => {
  if (!project.value?.generated_files) return null
  try {
    const parsed = JSON.parse(project.value.generated_files) as Partial<GeneratedWebsiteFiles>
    if (
      typeof parsed['index.html'] !== 'string' ||
      typeof parsed['style.css'] !== 'string' ||
      typeof parsed['script.js'] !== 'string'
    ) {
      return null
    }
    return parsed as GeneratedWebsiteFiles
  } catch {
    return null
  }
})

const previewDocument = computed(() => {
  const files = generatedFiles.value
  if (!files) return ''

  let html = files['index.html']
    .replace(/<link[^>]*href=["']style\.css["'][^>]*>/gi, '')
    .replace(/<script[^>]*src=["']script\.js["'][^>]*><\/script>/gi, '')
  const style = `<style>${files['style.css']}</style>`
  const closingScriptPattern = new RegExp('<' + '/script', 'gi')
  const safeJavaScript = files['script.js'].replace(closingScriptPattern, '<\\/script')
  const script = files['script.js'] ? '<scr' + `ipt>${safeJavaScript}</scr` + 'ipt>' : ''
  const editorSource = EDITOR_BRIDGE_SCRIPT.replace(closingScriptPattern, '<\\/script')
  const editorScript = designMode.value ? '<scr' + `ipt>${editorSource}</scr` + 'ipt>' : ''

  html = html.includes('</head>') ? html.replace('</head>', `${style}</head>`) : `${style}${html}`
  return html.includes('</body>')
    ? html.replace('</body>', `${script}${editorScript}</body>`)
    : `${html}${script}${editorScript}`
})

const selectionBoxStyle = computed(() => {
  const rect = selectedElement.value?.rect
  if (!rect) return {}
  return {
    left: `${rect.x}px`,
    top: `${rect.y}px`,
    width: `${rect.width}px`,
    height: `${rect.height}px`,
  }
})

const hoverBoxStyle = computed(() => {
  const rect = hoveredElement.value?.rect
  if (!rect) return {}
  return {
    left: `${rect.x}px`,
    top: `${rect.y}px`,
    width: `${rect.width}px`,
    height: `${rect.height}px`,
  }
})

const elementPromptStyle = computed(() => {
  const rect = selectedElement.value?.rect
  if (!rect) return {}
  const promptWidth = 360
  const promptHeight = 48
  const frameWidth = websiteFrame.value?.clientWidth || promptWidth + 16
  const frameHeight = websiteFrame.value?.clientHeight || rect.y + rect.height + promptHeight + 18
  const belowTop = rect.y + rect.height + 10
  const top =
    belowTop + promptHeight <= frameHeight ? belowTop : Math.max(8, rect.y - promptHeight - 10)
  return {
    left: `${Math.max(8, Math.min(rect.x, frameWidth - promptWidth - 8))}px`,
    top: `${top}px`,
    width: `${Math.min(promptWidth, frameWidth - 16)}px`,
  }
})

const showBuilderStatus = computed(() =>
  ['spec_approved', 'building', 'completed', 'build_failed', 'validation_failed'].includes(
    project.value?.status || '',
  ),
)

function handleEditorMessage(event: MessageEvent) {
  if (!designMode.value || event.source !== websiteFrame.value?.contentWindow) return
  const message = event.data as {
    source?: string
    type?: string
    payload?:
      | SelectedEditableElement
      | { elementId: string; rect: ElementRect }
      | { elementId: string; text: string; rect: ElementRect }
      | { elementId?: string }
  }
  if (message?.source !== 'forge-visual-editor') return
  if (message.type === 'element-deselected') {
    selectedElement.value = null
    hoveredElement.value = null
    return
  }
  if (!message.payload || !('elementId' in message.payload) || !message.payload.elementId) return
  if (message.type === 'element-hovered' && 'tagName' in message.payload) {
    if (!selectedElement.value) {
      hoveredElement.value = {
        elementId: message.payload.elementId,
        tagName: message.payload.tagName,
        rect: message.payload.rect,
      }
    }
    return
  }
  if (message.type === 'element-hover-cleared') {
    if (hoveredElement.value?.elementId === message.payload.elementId) {
      hoveredElement.value = null
    }
    return
  }
  if (message.type === 'element-changed' && 'text' in message.payload) {
    const { elementId, text, rect } = message.payload
    mergePendingEdit(elementId, { text }, { coalesceText: true })
    if (selectedElement.value?.elementId === elementId) {
      selectedElement.value = { ...selectedElement.value, text, rect }
    }
    editorError.value = null
    return
  }
  if (message.type === 'selection-position' && 'rect' in message.payload) {
    if (selectedElement.value?.elementId === message.payload.elementId) {
      selectedElement.value = {
        ...selectedElement.value,
        rect: message.payload.rect,
      }
    }
    return
  }
  if (message.type !== 'element-selected' || !('styles' in message.payload)) return
  hoveredElement.value = null
  const selectedPayload = message.payload as SelectedEditableElement
  const pending = pendingEdits.value[selectedPayload.elementId]?.changes
  selectedElement.value = {
    ...selectedPayload,
    text: typeof pending?.text === 'string' ? pending.text : selectedPayload.text,
    styles: {
      ...selectedPayload.styles,
      ...pending?.styles,
    },
  }
  editorError.value = null
}

/** Vue reactive proxies are not structured-cloneable (postMessage / structuredClone throw). */
function clonePlain<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function postEditorUpdate(elementId: string, changes: WebsiteElementPatch['changes']) {
  websiteFrame.value?.contentWindow?.postMessage(
    {
      source: 'forge-visual-editor-host',
      type: 'update-element',
      payload: clonePlain({ elementId, changes }),
    },
    '*',
  )
}

function mergePendingEdit(
  elementId: string,
  changes: WebsiteElementPatch['changes'],
  options: { coalesceText?: boolean } = {},
) {
  const textOnly =
    typeof changes.text === 'string' &&
    !changes.styles &&
    Object.keys(changes).every((key) => key === 'text')
  const shouldCoalesce =
    options.coalesceText &&
    textOnly &&
    lastHistoryWasTextFor.value === elementId &&
    editHistory.value.length > 0

  if (!shouldCoalesce) {
    editHistory.value.push(clonePlain(pendingEdits.value))
  }
  if (textOnly) {
    lastHistoryWasTextFor.value = elementId
  } else {
    lastHistoryWasTextFor.value = null
  }

  const current = pendingEdits.value[elementId]
  pendingEdits.value = {
    ...pendingEdits.value,
    [elementId]: {
      element_id: elementId,
      changes: {
        ...current?.changes,
        ...changes,
        styles: {
          ...current?.changes.styles,
          ...changes.styles,
        },
      },
    },
  }
}

function styleValue(property: EditableStyleName) {
  return selectedElement.value?.styles[property] || defaultStyleValue(property)
}

function defaultStyleValue(property: EditableStyleName) {
  if (['color', 'background-color', 'border-color'].includes(property)) return '#ffffff'
  if (property === 'font-weight') return '400'
  if (property === 'font-family') return 'sans-serif'
  if (property === 'text-align') return 'left'
  return '0px'
}

function cssNumber(property: EditableStyleName) {
  const value = Number.parseFloat(styleValue(property))
  return Number.isFinite(value) ? value : 0
}

function updateStyle(property: EditableStyleName, value: string) {
  const element = selectedElement.value
  if (!element) return
  const styles = { [property]: value }
  selectedElement.value = {
    ...element,
    styles: { ...element.styles, ...styles },
  }
  mergePendingEdit(element.elementId, { styles })
  postEditorUpdate(element.elementId, { styles })
}

function updateSelectedText(event: Event) {
  const element = selectedElement.value
  if (!element) return
  const text = (event.target as HTMLTextAreaElement).value
  selectedElement.value = { ...element, text }
  mergePendingEdit(element.elementId, { text }, { coalesceText: true })
  postEditorUpdate(element.elementId, { text })
}

function appendDesignAssistantMessage(content: string) {
  const text = content.trim() || '已收到，但暂时没有可用的回复内容。'
  designChatMessages.value = [...designChatMessages.value, { role: 'assistant', content: text }]
}

async function askAiToChangeElement() {
  const current = project.value
  const element = selectedElement.value
  const instruction = elementInstruction.value.trim()
  if (!current || !element || !instruction || suggestingElement.value) return

  const tag = element.tagName.toLowerCase()
  const history = designChatMessages.value.slice(-12)
  const apiHistory = history.map(({ role, content }) => ({ role, content }))

  // Chat-first: record the user turn and clear input before awaiting the API.
  designChatMessages.value = [...history, { role: 'user', content: instruction, tagName: tag }]
  elementInstruction.value = ''
  editorError.value = null
  designAskPending.value = true
  await nextTick()

  try {
    const reply = await projects.reviseWebsite(current.id, {
      instruction,
      history: apiHistory,
      base_revision: current.website_revision ?? 0,
      focus: {
        element_id: element.elementId,
        tag_name: tag,
        text: element.text,
        text_editable: element.textEditable,
        styles: element.styles,
      },
    })
    appendDesignAssistantMessage(
      reply.message || (reply.mode === 'applied' ? '好，已改好并保存。' : '还不太确定你想怎么改。'),
    )
    if (reply.mode === 'applied') {
      pendingEdits.value = {}
      editHistory.value = []
      lastHistoryWasTextFor.value = null
      selectedElement.value = null
      hoveredElement.value = null
      previewKey.value += 1
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : '修改失败，请重试'
    editorError.value = message
    appendDesignAssistantMessage(message)
  } finally {
    designAskPending.value = false
  }
}

function updateColorStyle(property: EditableStyleName, event: Event) {
  const value = (event.target as HTMLInputElement).value
  updateStyle(property, value)
}

function updatePixelStyle(property: EditableStyleName, event: Event) {
  const value = (event.target as HTMLInputElement).value
  updateStyle(property, `${value || 0}px`)
}

function updateSelectStyle(property: EditableStyleName, event: Event) {
  updateStyle(property, (event.target as HTMLSelectElement).value)
}

function undoVisualEdit() {
  const previous = editHistory.value.pop()
  if (!previous) return
  pendingEdits.value = previous
  lastHistoryWasTextFor.value = null
  selectedElement.value = null
  hoveredElement.value = null
  previewKey.value += 1
}

function clearCanvasSelection() {
  selectedElement.value = null
  hoveredElement.value = null
  websiteFrame.value?.contentWindow?.postMessage(
    {
      source: 'forge-visual-editor-host',
      type: 'clear-selection',
    },
    '*',
  )
}

function confirmDiscardPending(): boolean {
  if (!hasPendingEdits.value) return true
  return window.confirm('有未保存的设计修改，确定放弃吗？')
}

function handleDesignKeydown(event: KeyboardEvent) {
  if (!designMode.value) return
  if (event.key !== 'Escape') return
  const target = event.target as HTMLElement | null
  if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA')) return
  if (selectedElement.value) {
    event.preventDefault()
    clearCanvasSelection()
    return
  }
  event.preventDefault()
  if (!confirmDiscardPending()) return
  exitDesignMode()
}

function reapplyPendingEdits() {
  if (!designMode.value) return
  window.setTimeout(() => {
    Object.values(pendingEdits.value).forEach((patch) => {
      postEditorUpdate(patch.element_id, patch.changes)
    })
  }, 0)
}

function toggleDesignMode() {
  if (designMode.value) {
    if (!confirmDiscardPending()) return
    exitDesignMode()
    return
  }
  canvasView.value = 'preview'
  workspaceView.value = 'viewer'
  designMode.value = true
  selectedElement.value = null
  hoveredElement.value = null
  pendingEdits.value = {}
  editHistory.value = []
  lastHistoryWasTextFor.value = null
  editorError.value = null
  previewKey.value += 1
}

const selectionLabel = computed(() => {
  const element = selectedElement.value
  if (!element) return ''
  const fontSize = Number.parseFloat(element.styles['font-size'] || '')
  const sizeLabel = Number.isFinite(fontSize) ? `${Math.round(fontSize)}px` : ''
  return sizeLabel
    ? `${element.tagName.toLowerCase()} - ${sizeLabel}`
    : element.tagName.toLowerCase()
})

function discardVisualEdits() {
  if (!hasPendingEdits.value) return
  if (!window.confirm('有未保存的设计修改，确定放弃吗？')) return
  pendingEdits.value = {}
  editHistory.value = []
  lastHistoryWasTextFor.value = null
  selectedElement.value = null
  hoveredElement.value = null
  editorError.value = null
  elementInstruction.value = ''
  clearCanvasSelection()
  previewKey.value += 1
}

function exitDesignMode() {
  designMode.value = false
  selectedElement.value = null
  hoveredElement.value = null
  designChatMessages.value = []
  elementInstruction.value = ''
  editorError.value = null
  pendingEdits.value = {}
  editHistory.value = []
  lastHistoryWasTextFor.value = null
  editorError.value = null
  elementInstruction.value = ''
  previewKey.value += 1
}

async function saveVisualEdits() {
  const current = project.value
  const patches = Object.values(pendingEdits.value)
  if (!current || !patches.length || savingWebsite.value) return
  editorError.value = null
  try {
    await projects.editWebsite(current.id, {
      base_revision: current.website_revision,
      patches,
    })
    pendingEdits.value = {}
    editHistory.value = []
    lastHistoryWasTextFor.value = null
    selectedElement.value = null
    previewKey.value += 1
  } catch (err) {
    editorError.value = err instanceof Error ? err.message : '保存修改失败，请重试'
  }
}

onMounted(() => {
  window.addEventListener('message', handleEditorMessage)
  window.addEventListener('keydown', handleDesignKeydown)
})
onBeforeUnmount(() => {
  window.removeEventListener('message', handleEditorMessage)
  window.removeEventListener('keydown', handleDesignKeydown)
})

const validationIssues = computed<{ description: string }[]>(() => {
  if (!project.value?.validation_report) return []
  try {
    const report = JSON.parse(project.value.validation_report) as {
      attempts?: { issues?: { description: string }[] }[]
    }
    return report.attempts?.at(-1)?.issues || []
  } catch {
    return []
  }
})

const builderStepLabel = computed(() => {
  if (building.value || project.value?.status === 'building') return '正在处理第 2 步'
  if (project.value?.status === 'completed') return '已完成第 2 步'
  if (['build_failed', 'validation_failed'].includes(project.value?.status || '')) {
    return '第 2 步失败'
  }
  return '等待开始第 2 步'
})

const builderStatusText = computed(() => {
  if (building.value || project.value?.status === 'building') {
    return '正在根据已批准的页面区块生成 HTML、CSS 和 JavaScript。'
  }
  if (project.value?.status === 'completed') return '网站已构建完成，右侧可以查看真实预览。'
  if (project.value?.status === 'validation_failed') {
    return '网站未通过质量检查，请查看问题或重新构建。'
  }
  if (project.value?.status === 'build_failed') return '网站构建失败，请在右侧重新构建。'
  return '网站规格已批准，准备开始构建。'
})

const previewModes: { key: PreviewMode; label: string }[] = [
  { key: 'mobile', label: '显示手机预览' },
  { key: 'tablet', label: '显示平板预览' },
  { key: 'desktop', label: '显示桌面预览' },
]

const previewPages = computed(() => {
  const pages = websiteSpec.value?.site.pages
  if (!pages?.length) return [{ id: 'home', name: 'Home', path: '/' }]
  return pages.map((page) => ({ id: page.id, name: page.name, path: page.path }))
})

const consoleText = computed(() => {
  const lines = [
    `[status] ${project.value?.status || 'unknown'}`,
    `[revision] v${project.value?.website_revision ?? 0}`,
    workflowId.value ? `[workflow] ${workflowId.value}` : null,
    project.value?.build_error ? `[build_error] ${project.value.build_error}` : null,
    validationIssues.value.length
      ? `[validation] ${validationIssues.value.map((item) => item.description).join('; ')}`
      : null,
    `[preview] ${workspaceView.value} / ${previewMode.value}`,
  ]
  return lines.filter(Boolean).join('\n')
})

function setWorkspaceView(view: WorkspaceView) {
  workspaceView.value = view
  if (view !== 'viewer') {
    exitDesignMode()
  }
}

function refreshPreview() {
  previewKey.value += 1
}

async function shareProject() {
  const url = window.location.href
  try {
    await navigator.clipboard.writeText(url)
    window.alert('项目链接已复制到剪贴板。')
  } catch {
    window.prompt('复制以下链接分享项目：', url)
  }
}

function publishProject() {
  if (project.value?.status !== 'completed') {
    window.alert('网站构建完成后才能发布。')
    return
  }
  workspaceView.value = 'overview'
  window.alert('发布流程为演示模式：可在概览页管理线上版本与域名。')
}

function downloadGeneratedFile(name: 'index.html' | 'style.css' | 'script.js') {
  const files = generatedFiles.value
  if (!files) return
  const blob = new Blob([files[name]], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
  URL.revokeObjectURL(url)
}

function downloadAllGeneratedFiles() {
  const files = generatedFiles.value
  if (!files) return
  const payload = JSON.stringify(files, null, 2)
  const blob = new Blob([payload], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${project.value?.name || 'website'}-files.json`
  anchor.click()
  URL.revokeObjectURL(url)
}

const statusLabel = computed(() => {
  const map: Record<string, string> = {
    draft: '草稿',
    running: '构建中',
    prd_ready: '规格已完成',
    spec_approved: '规格已批准',
    building: '网站构建中',
    completed: '已完成',
    build_failed: '构建失败',
    validation_failed: '验证失败',
    failed: '失败',
  }
  return map[project.value?.status || ''] || project.value?.status || '—'
})

const stepLabel = computed(() => {
  if (starting.value || project.value?.status === 'running') return '正在处理第 1 步'
  if (project.value?.status === 'prd_ready') return '已处理 1 步'
  if (project.value?.status === 'spec_approved') return '已批准第 1 步'
  if (project.value?.status === 'failed') return '第 1 步失败'
  return '等待开始'
})

const agentStatusText = computed(() => {
  if (starting.value || project.value?.status === 'running') {
    return '正在理解需求并整理网站规格，请稍候…'
  }
  if (project.value?.status === 'failed') {
    return projects.error || '网站规格生成失败，可以点击右侧重新生成。'
  }
  if (project.value?.prd) {
    return planApproved.value
      ? '计划已确认。右侧可查看完整网站规格。'
      : '网站规格已生成。请确认左侧页面范围后继续。'
  }
  return '准备开始需求分析。'
})

const showPlanCard = computed(
  () =>
    Boolean(websiteSpec.value) &&
    (project.value?.status === 'prd_ready' || project.value?.status === 'spec_approved'),
)

const selectedCount = computed(() => planItems.filter((item) => item.checked).length)

function extractPlanItems(prd: string): PlanItem[] {
  try {
    const specification = JSON.parse(prd) as WebsiteSpecification
    const items = specification.site.pages.flatMap((page) =>
      page.sections.map((section) => ({
        id: `${page.id}-${section.id}`,
        pageId: page.id,
        sectionId: section.id,
        label: `${page.name} · ${section.title}`,
        checked: true,
      })),
    )
    if (items.length) return items.slice(0, 12)
  } catch {
    // Keep compatibility with projects generated using the former Markdown PRD.
  }

  const fromTree = [...prd.matchAll(/^[ \t]*[│├└].*?[├└]──\s*(.+)$/gm)]
    .map((m) => m[1]?.trim())
    .filter((label): label is string => Boolean(label))
    .filter((label) => label.length >= 2 && label.length <= 40)

  const unique = [...new Set(fromTree)].slice(0, 8)
  if (unique.length >= 3) {
    return unique.map((label, index) => ({
      id: `f-${index}`,
      pageId: null,
      sectionId: null,
      label,
      checked: true,
    }))
  }

  return [
    { id: 'home', pageId: null, sectionId: null, label: '首页 / 落地页', checked: true },
    { id: 'detail', pageId: null, sectionId: null, label: '内容详情页', checked: true },
    { id: 'about', pageId: null, sectionId: null, label: '关于 / 介绍页', checked: true },
    { id: 'nav', pageId: null, sectionId: null, label: '导航与整体布局', checked: true },
    { id: 'list', pageId: null, sectionId: null, label: '列表 / 分类筛选', checked: true },
  ]
}

function rebuildPlan(prd: string | null | undefined) {
  planItems.splice(0, planItems.length, ...extractPlanItems(prd || ''))
  planApproved.value = project.value?.status === 'spec_approved'
  approveError.value = null
}

function resetPlan() {
  planItems.forEach((item) => {
    item.checked = true
  })
  planApproved.value = false
}

async function approvePlan() {
  if (!selectedCount.value || !project.value || approving.value) return

  const selectedSections = planItems
    .filter(
      (item): item is PlanItem & { pageId: string; sectionId: string } =>
        item.checked && Boolean(item.pageId) && Boolean(item.sectionId),
    )
    .map((item) => ({ page_id: item.pageId, section_id: item.sectionId }))

  if (!selectedSections.length) {
    approveError.value = '旧版 PRD 无法批准，请重新生成网站规格。'
    return
  }

  approveError.value = null
  try {
    await projects.approveSpec(project.value.id, selectedSections)
    rebuildPlan(projects.current?.approved_spec)
    await runBuild()
  } catch (err) {
    approveError.value = err instanceof Error ? err.message : '批准失败，请重试'
  }
}

async function runBuild() {
  const id = Number(route.params.id)
  if (!Number.isFinite(id) || building.value) return
  try {
    await projects.buildProject(id)
    if (Number(route.params.id) === id) canvasView.value = 'preview'
  } catch {
    // The store refreshes the project so the persisted build error is shown.
  }
}

async function sendFollowUp() {
  const text = followUp.value.trim()
  const current = project.value
  if (!text || !current || revisingWebsite.value || starting.value || building.value) return

  followUp.value = ''
  const history = siteChatMessages.value.slice(-12)
  siteChatMessages.value = [...history, { role: 'user', content: text }]
  void scrollThread()

  if (current.status === 'completed' && current.generated_files) {
    try {
      const reply = await projects.reviseWebsite(current.id, {
        instruction: text,
        history,
        base_revision: current.website_revision ?? 0,
      })
      siteChatMessages.value = [
        ...siteChatMessages.value,
        { role: 'assistant', content: reply.message },
      ]
      if (reply.mode === 'applied') {
        pendingEdits.value = {}
        editHistory.value = []
        lastHistoryWasTextFor.value = null
        selectedElement.value = null
        hoveredElement.value = null
        previewKey.value += 1
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : '整站修改失败，请重试'
      siteChatMessages.value = [...siteChatMessages.value, { role: 'assistant', content: message }]
    }
    void scrollThread()
    return
  }

  siteChatMessages.value = [
    ...siteChatMessages.value,
    {
      role: 'assistant',
      content: '网站尚未构建完成。请先完成规格批准与构建；构建完成后可在此对话中修改整个网站。',
    },
  ]
  void scrollThread()
}

async function scrollThread() {
  await nextTick()
  const el = threadRef.value
  if (el) el.scrollTop = el.scrollHeight
}

async function retryStart() {
  const id = Number(route.params.id)
  if (!Number.isFinite(id)) return
  try {
    await projects.startProject(id, project.value?.prompt || undefined)
    rebuildPlan(projects.current?.prd)
  } catch {
    await projects.fetchOne(id).catch(() => undefined)
  } finally {
    await scrollThread()
  }
}

async function ensureStarted(expectedId: number) {
  const current = projects.current
  if (!current || current.id !== expectedId) return
  if (current.status === 'spec_approved' && current.approved_spec) {
    rebuildPlan(current.approved_spec)
    return
  }
  if (current.status === 'prd_ready' && current.prd) {
    rebuildPlan(current.prd)
    return
  }
  if (current.status === 'running') return
  if (current.status === 'draft' || current.status === 'failed') {
    try {
      await projects.startProject(current.id, current.prompt || undefined)
      if (Number(route.params.id) !== expectedId) return
      rebuildPlan(projects.current?.prd)
    } catch {
      if (Number(route.params.id) !== expectedId) return
      await projects.fetchOne(expectedId).catch(() => undefined)
    }
  }
}

async function ensureBuilt(expectedId: number) {
  const current = projects.current
  if (!current || current.id !== expectedId) return
  if (current.status === 'completed' && current.generated_files) {
    canvasView.value = 'preview'
    return
  }
  if (current.status === 'spec_approved' && current.approved_spec) {
    await runBuild()
  }
}

watch(
  () => project.value?.prd,
  (prd) => {
    if (prd && project.value?.status === 'prd_ready') {
      rebuildPlan(prd)
      void scrollThread()
    }
  },
)

let loadSequence = 0

async function loadProject(id: number) {
  const sequence = ++loadSequence
  bootLoading.value = true
  bootError.value = null
  planApproved.value = false
  approveError.value = null
  siteChatMessages.value = []
  designChatMessages.value = []
  elementInstruction.value = ''
  planItems.splice(0, planItems.length)
  canvasView.value = 'spec'
  projects.workflowId = null

  try {
    await projects.fetchOne(id)
    if (Number(route.params.id) !== id || sequence !== loadSequence) return
    bootLoading.value = false
    if (projects.current?.status === 'spec_approved' && projects.current.approved_spec) {
      rebuildPlan(projects.current.approved_spec)
    }
    await scrollThread()
    await ensureStarted(id)
    if (Number(route.params.id) !== id || sequence !== loadSequence) return
    await ensureBuilt(id)
    if (Number(route.params.id) !== id || sequence !== loadSequence) return
    await scrollThread()
  } catch (err) {
    if (Number(route.params.id) !== id || sequence !== loadSequence) return
    bootError.value = err instanceof Error ? err.message : '加载失败'
    bootLoading.value = false
  }
}

watch(
  () => Number(route.params.id),
  (id) => {
    if (!Number.isFinite(id)) {
      bootError.value = '无效的项目 ID'
      bootLoading.value = false
      return
    }
    void loadProject(id)
  },
  { immediate: true },
)
</script>

<style scoped src="./project-view.scss" lang="scss"></style>
