<template>
  <div class="workbench" :class="{ 'atoms-mode': generatedFiles, 'design-mode': designMode }">
    <ProjectTopbar v-if="generatedFiles" :name="project?.name" :status="project?.status" />
    <aside class="chat-pane">
      <header v-show="!designMode" class="chat-header">
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
          <div v-if="project?.prompt" class="bubble user">
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
            <p v-if="workflowId" class="workflow-id">workflow: {{ workflowId }}</p>
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

          <div v-if="showPlanCard" class="plan-card">
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

          <div v-for="(note, index) in followUpNotes" :key="`note-${index}`" class="bubble user">
            <p>{{ note }}</p>
          </div>
        </template>
      </div>

      <footer v-show="!designMode" class="chat-composer">
        <textarea
          v-model="followUp"
          rows="2"
          placeholder="继续补充需求或修改计划…"
          :disabled="!project || starting"
          @keydown.enter.exact.prevent="sendFollowUp"
        />
        <button
          type="button"
          class="send"
          title="发送"
          :disabled="!followUp.trim() || starting"
          @click="sendFollowUp"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 19V5" />
            <path d="m6 11 6-6 6 6" />
          </svg>
        </button>
      </footer>

      <div v-if="designMode" class="visual-editor">
        <header class="visual-editor-head">
          <nav class="editor-tabs" aria-label="设计工具">
            <button
              v-for="tab in editorTabs"
              :key="tab.key"
              type="button"
              :class="{ active: editorTab === tab.key }"
              @click="editorTab = tab.key"
            >
              {{ tab.label }}
            </button>
          </nav>
          <button type="button" class="tool-btn" @click="exitDesignMode">退出</button>
        </header>

        <div v-if="editorTab !== 'visual'" class="editor-empty editor-placeholder">
          <span class="editor-cursor" aria-hidden="true">◇</span>
          <strong>{{
            editorTab === 'library' ? '组件库将在下一阶段接入' : '全局主题将在下一阶段接入'
          }}</strong>
          <p>
            {{
              editorTab === 'library'
                ? '这里将用于插入图标、图片和预制组件。'
                : '这里将统一管理颜色、字体、间距和圆角变量。'
            }}
          </p>
        </div>

        <div v-else-if="!selectedElement" class="editor-empty">
          <span class="editor-cursor" aria-hidden="true">⌖</span>
          <strong>点击右侧网页中的元素</strong>
          <p>单击选择元素，双击文字即可在网页中直接修改。</p>
        </div>

        <div v-else-if="editorTab === 'visual'" class="editor-controls">
          <div class="selected-element-toolbar">
            <div class="selected-element-meta">
              <span>T&nbsp; {{ selectedElement.tagName.toLowerCase() }}</span>
              <code
                >{{ Math.round(selectedElement.rect.width) }} ×
                {{ Math.round(selectedElement.rect.height) }}</code
              >
            </div>
            <button
              type="button"
              class="editor-icon-btn"
              title="撤销上一步"
              :disabled="!editHistory.length"
              @click="undoVisualEdit"
            >
              ↶
            </button>
          </div>

          <section class="editor-section">
            <h3>Content</h3>
            <p v-if="selectedElement.textEditable" class="editor-note">
              可在这里修改，也可以双击右侧文字直接编辑。
            </p>
            <label v-if="selectedElement.textEditable" class="editor-field">
              <textarea
                :value="selectedElement.text"
                rows="4"
                maxlength="2000"
                @input="updateSelectedText"
              />
            </label>
            <p v-else class="editor-note">该元素包含子元素，暂不支持直接替换文字。</p>
          </section>

          <section class="editor-section">
            <h3>Typography</h3>
            <div class="editor-grid two-columns">
              <label class="compact-field">
                <span>Size</span>
                <input
                  type="number"
                  min="8"
                  max="200"
                  :value="cssNumber('font-size')"
                  @input="updatePixelStyle('font-size', $event)"
                />
              </label>
              <label class="compact-field">
                <span>Weight</span>
                <select
                  :value="styleValue('font-weight')"
                  @change="updateSelectStyle('font-weight', $event)"
                >
                  <option v-for="weight in fontWeights" :key="weight" :value="weight">
                    {{ weight }}
                  </option>
                </select>
              </label>
              <label class="compact-field full-width">
                <span>Font</span>
                <select
                  :value="styleValue('font-family')"
                  @change="updateSelectStyle('font-family', $event)"
                >
                  <option v-for="font in fontFamilies" :key="font" :value="font">
                    {{ font }}
                  </option>
                </select>
              </label>
            </div>
            <div class="alignment-control" aria-label="文本对齐">
              <button
                v-for="alignment in textAlignments"
                :key="alignment.value"
                type="button"
                :class="{ active: styleValue('text-align') === alignment.value }"
                @click="updateStyle('text-align', alignment.value)"
              >
                {{ alignment.icon }}
              </button>
            </div>
          </section>

          <section class="editor-section">
            <h3>Appearance</h3>
            <div class="editor-grid three-columns">
              <label
                v-for="colorControl in colorControls"
                :key="colorControl.property"
                class="compact-field color-compact"
              >
                <span>{{ colorControl.label }}</span>
                <input
                  type="color"
                  :value="styleValue(colorControl.property)"
                  @input="updateColorStyle(colorControl.property, $event)"
                />
              </label>
            </div>
          </section>

          <section class="editor-section">
            <h3>Layout</h3>
            <p class="field-group-label">Margin</p>
            <div class="editor-grid four-columns">
              <label v-for="side in boxSides" :key="`margin-${side}`" class="compact-field">
                <span>{{ side.slice(0, 1).toUpperCase() }}</span>
                <input
                  type="number"
                  :value="cssNumber(`margin-${side}`)"
                  @input="updatePixelStyle(`margin-${side}`, $event)"
                />
              </label>
            </div>
            <p class="field-group-label">Padding</p>
            <div class="editor-grid four-columns">
              <label v-for="side in boxSides" :key="`padding-${side}`" class="compact-field">
                <span>{{ side.slice(0, 1).toUpperCase() }}</span>
                <input
                  type="number"
                  min="0"
                  :value="cssNumber(`padding-${side}`)"
                  @input="updatePixelStyle(`padding-${side}`, $event)"
                />
              </label>
            </div>
            <div class="editor-grid two-columns layout-tail">
              <label class="compact-field">
                <span>Gap</span>
                <input
                  type="number"
                  min="0"
                  :value="cssNumber('gap')"
                  @input="updatePixelStyle('gap', $event)"
                />
              </label>
              <label class="compact-field">
                <span>Radius</span>
                <input
                  type="number"
                  min="0"
                  :value="cssNumber('border-radius')"
                  @input="updatePixelStyle('border-radius', $event)"
                />
              </label>
            </div>
          </section>
        </div>

        <p v-if="editorError" class="plan-error editor-error">{{ editorError }}</p>
        <section class="element-ai-composer" :class="{ selected: selectedElement }">
          <div class="element-ai-context">
            <span class="element-ai-mode">✣ Design</span>
            <span v-if="selectedElement" class="element-ai-tag">
              T&nbsp; {{ selectedElement.tagName.toLowerCase() }}
            </span>
          </div>
          <div class="element-ai-input-row">
            <textarea
              v-model="elementInstruction"
              rows="2"
              maxlength="2000"
              :disabled="!selectedElement || suggestingElement"
              :placeholder="
                selectedElement ? '告诉 AI 如何修改当前元素…' : '请先在网页中选择一个元素'
              "
              @keydown.enter.exact.prevent="askAiToChangeElement"
            />
            <button
              type="button"
              class="element-ai-send"
              :disabled="!selectedElement || !elementInstruction.trim() || suggestingElement"
              title="修改当前元素"
              @click="askAiToChangeElement"
            >
              {{ suggestingElement ? '…' : '↑' }}
            </button>
          </div>
          <p class="element-ai-hint">
            {{
              selectedElement
                ? 'AI 只会修改当前选中的元素，确认效果后再保存。'
                : '选中元素后，可让 AI 修改文案、颜色、字体和间距。'
            }}
          </p>
        </section>
      </div>
    </aside>

    <section class="canvas-pane">
      <CanvasToolbar
        v-model:preview-mode="previewMode"
        :generated="Boolean(generatedFiles)"
        :design-mode="designMode"
        :status="project?.status"
        :status-label="statusLabel"
        :modes="previewModes"
        @toggle-design="toggleDesignMode"
      />

      <div
        class="canvas-body"
        :class="[previewMode, { 'website-mode': generatedFiles && canvasView === 'preview' }]"
      >
        <div v-if="building || project?.status === 'building'" class="canvas-empty">
          <div class="build-spinner" aria-hidden="true" />
          <p class="empty-title">Website Builder 正在构建网站…</p>
          <p class="empty-desc">正在生成页面结构、视觉样式和本地交互，请稍候。</p>
        </div>
        <div
          v-else-if="project?.status === 'build_failed' || project?.status === 'validation_failed'"
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
              {{ hoveredElement.tagName.toLowerCase() }} ·
              {{ Math.round(hoveredElement.rect.width) }} ×
              {{ Math.round(hoveredElement.rect.height) }}
            </span>
          </div>
          <div
            v-if="designMode && selectedElement"
            class="canvas-selection-box"
            :style="selectionBoxStyle"
          >
            <span class="canvas-selection-label">
              {{ selectedElement.tagName.toLowerCase() }} ·
              {{ Math.round(selectedElement.rect.width) }} ×
              {{ Math.round(selectedElement.rect.height) }}
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
              placeholder="让 AI 修改当前元素…"
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
          <p class="empty-desc">{{ projects.error || '请返回首页重试，或在左侧继续补充需求。' }}</p>
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
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import type {
  EditableStyleName,
  GeneratedWebsiteFiles,
  WebsiteElementPatch,
  WebsiteSpecification,
} from '@/api/modules/project'
import ForgeLogo from '@/components/ForgeLogo.vue'
import { useProjectStore } from '@/stores'
import CanvasToolbar from '../components/CanvasToolbar.vue'
import ProjectTopbar from '../components/ProjectTopbar.vue'
import { EDITOR_BRIDGE_SCRIPT } from '../composables/editorBridge'
import type {
  CanvasView,
  EditorTab,
  ElementRect,
  PlanItem,
  PreviewMode,
  SelectedEditableElement,
} from '../types/projectView'

const route = useRoute()
const projects = useProjectStore()

const bootLoading = ref(true)
const bootError = ref<string | null>(null)
const followUp = ref('')
const planApproved = ref(false)
const approveError = ref<string | null>(null)
const planItems = reactive<PlanItem[]>([])
const previewMode = ref<PreviewMode>('desktop')
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
const editorError = ref<string | null>(null)
const elementInstruction = ref('')
const previewKey = ref(0)
const starting = computed(() => projects.starting)
const approving = computed(() => projects.approving)
const building = computed(() => projects.building)
const savingWebsite = computed(() => projects.savingWebsite)
const suggestingElement = computed(() => projects.suggestingElement)
const hasPendingEdits = computed(() => Object.keys(pendingEdits.value).length > 0)

const editorTabs: { key: EditorTab; label: string }[] = [
  { key: 'visual', label: 'Visual Editor' },
  { key: 'library', label: 'Library' },
  { key: 'theme', label: 'Theme' },
]
const fontWeights = ['100', '200', '300', '400', '500', '600', '700', '800', '900']
const fontFamilies = ['Inter', 'Arial', 'Georgia', 'system-ui', 'sans-serif', 'serif']
const textAlignments = [
  { value: 'left', icon: '≡' },
  { value: 'center', icon: '≣' },
  { value: 'right', icon: '≡' },
  { value: 'justify', icon: '☰' },
] as const
const colorControls: { label: string; property: EditableStyleName }[] = [
  { label: 'Text', property: 'color' },
  { label: 'Fill', property: 'background-color' },
  { label: 'Border', property: 'border-color' },
]
const boxSides = ['top', 'right', 'bottom', 'left'] as const

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
  }
  if (message?.source !== 'forge-visual-editor' || !message.payload?.elementId) return
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
    mergePendingEdit(elementId, { text })
    if (selectedElement.value?.elementId === elementId) {
      selectedElement.value = { ...selectedElement.value, text, rect }
    }
    editorError.value = null
    return
  }
  if (message.type === 'selection-position') {
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
  const pendingText = pendingEdits.value[selectedPayload.elementId]?.changes.text
  selectedElement.value =
    typeof pendingText === 'string' ? { ...selectedPayload, text: pendingText } : selectedPayload
  editorError.value = null
}

function postEditorUpdate(elementId: string, changes: WebsiteElementPatch['changes']) {
  websiteFrame.value?.contentWindow?.postMessage(
    {
      source: 'forge-visual-editor-host',
      type: 'update-element',
      payload: { elementId, changes },
    },
    '*',
  )
}

function mergePendingEdit(elementId: string, changes: WebsiteElementPatch['changes']) {
  editHistory.value.push(structuredClone(pendingEdits.value))
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
  mergePendingEdit(element.elementId, { text })
  postEditorUpdate(element.elementId, { text })
}

async function askAiToChangeElement() {
  const current = project.value
  const element = selectedElement.value
  const instruction = elementInstruction.value.trim()
  if (!current || !element || !instruction || suggestingElement.value) return
  editorError.value = null
  try {
    const patch = await projects.suggestElementEdit(current.id, {
      element_id: element.elementId,
      tag_name: element.tagName.toLowerCase(),
      text: element.text,
      text_editable: element.textEditable,
      styles: element.styles,
      instruction,
    })
    mergePendingEdit(patch.element_id, patch.changes)
    postEditorUpdate(patch.element_id, patch.changes)
    if (patch.changes.text !== undefined) {
      selectedElement.value = { ...element, text: patch.changes.text ?? element.text }
    }
    elementInstruction.value = ''
  } catch (err) {
    editorError.value = err instanceof Error ? err.message : 'AI 修改当前元素失败，请重试'
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
  selectedElement.value = null
  hoveredElement.value = null
  previewKey.value += 1
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
    exitDesignMode()
    return
  }
  canvasView.value = 'preview'
  designMode.value = true
  selectedElement.value = null
  hoveredElement.value = null
  pendingEdits.value = {}
  editHistory.value = []
  editorError.value = null
  previewKey.value += 1
}

function discardVisualEdits() {
  pendingEdits.value = {}
  editHistory.value = []
  selectedElement.value = null
  hoveredElement.value = null
  editorError.value = null
  elementInstruction.value = ''
  previewKey.value += 1
}

function exitDesignMode() {
  designMode.value = false
  selectedElement.value = null
  hoveredElement.value = null
  pendingEdits.value = {}
  editHistory.value = []
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
    selectedElement.value = null
    previewKey.value += 1
  } catch (err) {
    editorError.value = err instanceof Error ? err.message : '保存修改失败，请重试'
  }
}

onMounted(() => window.addEventListener('message', handleEditorMessage))
onBeforeUnmount(() => window.removeEventListener('message', handleEditorMessage))

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

const previewModes: { key: PreviewMode; label: string; icon: string }[] = [
  { key: 'desktop', label: '桌面', icon: '🖥' },
  { key: 'tablet', label: '平板', icon: '▤' },
  { key: 'mobile', label: '手机', icon: '▢' },
]

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

const followUpNotes = ref<string[]>([])

function sendFollowUp() {
  const text = followUp.value.trim()
  if (!text) return
  followUpNotes.value.push(text)
  followUp.value = ''
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
  followUpNotes.value = []
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

<style scoped src="../styles/project-view.scss" lang="scss"></style>
