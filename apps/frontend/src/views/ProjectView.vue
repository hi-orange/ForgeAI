<template>
  <div class="workbench">
    <aside class="chat-pane">
      <header class="chat-header">
        <RouterLink class="home-link" :to="{ name: 'home' }" title="返回首页">
          <ForgeLogo :size="22" />
        </RouterLink>
        <div class="chat-title-wrap">
          <p class="chat-eyebrow">工作台</p>
          <h1 class="chat-title">{{ project?.name || '新项目' }}</h1>
        </div>
      </header>

      <div ref="threadRef" class="chat-thread">
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

      <footer class="chat-composer">
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
    </aside>

    <section class="canvas-pane">
      <header class="canvas-toolbar">
        <div class="toolbar-left">
          <span class="viewer-label">应用查看器</span>
          <span :class="['status-pill', project?.status || 'draft']">{{ statusLabel }}</span>
        </div>
        <div class="toolbar-center">
          <button
            v-for="mode in previewModes"
            :key="mode.key"
            type="button"
            class="icon-btn"
            :class="{ active: previewMode === mode.key }"
            :title="mode.label"
            @click="previewMode = mode.key"
          >
            {{ mode.icon }}
          </button>
        </div>
        <div class="toolbar-right">
          <button
            v-if="generatedFiles"
            type="button"
            class="tool-btn"
            @click="canvasView = canvasView === 'preview' ? 'spec' : 'preview'"
          >
            {{ canvasView === 'preview' ? '查看规格' : '查看网站' }}
          </button>
          <button type="button" class="tool-btn">分享</button>
          <button
            type="button"
            class="tool-btn primary"
            :disabled="project?.status !== 'completed'"
          >
            发布
          </button>
        </div>
      </header>

      <div class="canvas-body" :class="previewMode">
        <div v-if="building || project?.status === 'building'" class="canvas-empty">
          <div class="build-spinner" aria-hidden="true" />
          <p class="empty-title">Website Builder 正在构建网站…</p>
          <p class="empty-desc">正在生成页面结构、视觉样式和本地交互，请稍候。</p>
        </div>
        <div v-else-if="project?.status === 'build_failed'" class="canvas-empty">
          <p class="empty-title">网站构建失败</p>
          <p class="empty-desc">{{ project.build_error || projects.error || '请稍后重试。' }}</p>
          <button type="button" class="btn primary" @click="runBuild">重新构建</button>
        </div>
        <div v-else-if="generatedFiles && canvasView === 'preview'" class="website-preview-shell">
          <iframe
            class="website-frame"
            title="生成的网站预览"
            sandbox="allow-scripts"
            :srcdoc="previewDocument"
          />
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
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import type { GeneratedWebsiteFiles, WebsiteSpecification } from '@/api/projects'
import ForgeLogo from '@/components/ForgeLogo.vue'
import { useProjectStore } from '@/stores/project'

type PlanItem = {
  id: string
  pageId: string | null
  sectionId: string | null
  label: string
  checked: boolean
}
type PreviewMode = 'desktop' | 'tablet' | 'mobile'
type CanvasView = 'preview' | 'spec'

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
const starting = computed(() => projects.starting)
const approving = computed(() => projects.approving)
const building = computed(() => projects.building)

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
  // Generated components commonly use the native `hidden` attribute for dialogs.
  // Preserve its semantics even when generated class styles set `display`.
  const style = `<style>[hidden] { display: none !important; }\n${files['style.css']}</style>`
  const closingScriptPattern = new RegExp('<' + '/script', 'gi')
  const safeJavaScript = files['script.js'].replace(closingScriptPattern, '<\\/script')
  const script = files['script.js'] ? '<scr' + `ipt>${safeJavaScript}</scr` + 'ipt>' : ''

  html = html.includes('</head>') ? html.replace('</head>', `${style}</head>`) : `${style}${html}`
  return html.includes('</body>') ? html.replace('</body>', `${script}</body>`) : `${html}${script}`
})

const showBuilderStatus = computed(() =>
  ['spec_approved', 'building', 'completed', 'build_failed'].includes(project.value?.status || ''),
)

const builderStepLabel = computed(() => {
  if (building.value || project.value?.status === 'building') return '正在处理第 2 步'
  if (project.value?.status === 'completed') return '已完成第 2 步'
  if (project.value?.status === 'build_failed') return '第 2 步失败'
  return '等待开始第 2 步'
})

const builderStatusText = computed(() => {
  if (building.value || project.value?.status === 'building') {
    return '正在根据已批准的页面区块生成 HTML、CSS 和 JavaScript。'
  }
  if (project.value?.status === 'completed') return '网站已构建完成，右侧可以查看真实预览。'
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

async function loadProject(id: number) {
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
    if (Number(route.params.id) !== id) return
    bootLoading.value = false
    if (projects.current?.status === 'spec_approved' && projects.current.approved_spec) {
      rebuildPlan(projects.current.approved_spec)
    }
    await scrollThread()
    await ensureStarted(id)
    if (Number(route.params.id) !== id) return
    await ensureBuilt(id)
    if (Number(route.params.id) !== id) return
    await scrollThread()
  } catch (err) {
    if (Number(route.params.id) !== id) return
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

<style scoped>
.workbench {
  display: grid;
  grid-template-columns: minmax(300px, 360px) 1fr;
  height: 100vh;
  background: #eef1f6;
  color: #0f172a;
}

.chat-pane {
  display: flex;
  flex-direction: column;
  min-width: 0;
  border-right: 1px solid #e2e8f0;
  background: #f8fafc;
}

.chat-header {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.9rem 1rem;
  border-bottom: 1px solid #e8edf5;
  background: #fff;
}

.home-link {
  display: grid;
  place-items: center;
  width: 2.1rem;
  height: 2.1rem;
  border-radius: 0.65rem;
  background: #eff6ff;
  text-decoration: none;
}

.chat-eyebrow {
  margin: 0;
  color: #94a3b8;
  font-size: 0.72rem;
  font-weight: 600;
}

.chat-title {
  margin: 0.1rem 0 0;
  font-size: 0.95rem;
  font-weight: 700;
  line-height: 1.3;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 240px;
}

.chat-thread {
  flex: 1;
  overflow: auto;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}

.thread-hint,
.thread-error {
  margin: 0;
  font-size: 0.9rem;
}

.thread-error {
  color: #dc2626;
  font-weight: 600;
}

.bubble {
  max-width: 100%;
  padding: 0.85rem 0.95rem;
  border-radius: 1rem;
  line-height: 1.55;
  font-size: 0.92rem;
}

.bubble.user {
  align-self: flex-end;
  background: #2563eb;
  color: #fff;
  border-bottom-right-radius: 0.35rem;
}

.bubble.user p {
  margin: 0;
  white-space: pre-wrap;
}

.bubble.agent {
  align-self: stretch;
  background: #fff;
  border: 1px solid #e6ebf2;
  border-bottom-left-radius: 0.35rem;
}

.agent-meta {
  display: flex;
  gap: 0.65rem;
  align-items: center;
  margin-bottom: 0.55rem;
}

.agent-avatar {
  display: grid;
  place-items: center;
  width: 2rem;
  height: 2rem;
  border-radius: 999px;
  background: #dbeafe;
  color: #1d4ed8;
  font-size: 0.72rem;
  font-weight: 800;
}

.builder-avatar {
  background: #ede9fe;
  color: #6d28d9;
}

.builder-bubble {
  border-color: #ddd6fe;
}

.agent-meta strong {
  display: block;
  font-size: 0.88rem;
}

.agent-step {
  margin: 0.1rem 0 0;
  color: #64748b;
  font-size: 0.75rem;
}

.agent-text {
  margin: 0;
  color: #334155;
}

.workflow-id {
  margin: 0.55rem 0 0;
  color: #94a3b8;
  font-size: 0.72rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.plan-card {
  padding: 0.95rem;
  border: 1px solid #e2e8f0;
  border-radius: 1rem;
  background: #fff;
  box-shadow: 0 10px 24px rgba(15, 23, 42, 0.04);
}

.plan-intro {
  margin: 0 0 0.75rem;
  color: #475569;
  font-size: 0.88rem;
  line-height: 1.5;
}

.plan-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.45rem;
}

.plan-list label {
  display: flex;
  align-items: flex-start;
  gap: 0.55rem;
  color: #0f172a;
  font-size: 0.88rem;
  cursor: pointer;
}

.plan-list input {
  margin-top: 0.2rem;
}

.plan-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 0.9rem;
}

.plan-done {
  margin: 0.7rem 0 0;
  color: #15803d;
  font-size: 0.82rem;
  font-weight: 600;
}

.plan-error {
  margin: 0.7rem 0 0;
  color: #dc2626;
  font-size: 0.82rem;
  font-weight: 600;
}

.btn {
  border: 0;
  border-radius: 0.7rem;
  padding: 0.45rem 0.85rem;
  font-size: 0.86rem;
  font-weight: 700;
  cursor: pointer;
}

.btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.btn.ghost {
  background: #f1f5f9;
  color: #475569;
}

.btn.primary {
  background: #2563eb;
  color: #fff;
}

.chat-composer {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0.55rem;
  align-items: end;
  padding: 0.85rem;
  border-top: 1px solid #e8edf5;
  background: #fff;
}

.chat-composer textarea {
  width: 100%;
  resize: none;
  border: 1px solid #e2e8f0;
  border-radius: 0.85rem;
  padding: 0.7rem 0.8rem;
  font: inherit;
  color: #0f172a;
  background: #f8fafc;
}

.chat-composer textarea:focus {
  outline: 2px solid rgba(37, 99, 235, 0.2);
  border-color: #93c5fd;
}

.send {
  display: grid;
  place-items: center;
  width: 2.35rem;
  height: 2.35rem;
  border: 0;
  border-radius: 999px;
  background: #2563eb;
  color: #fff;
  cursor: pointer;
}

.send:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.send svg {
  width: 1.05rem;
  height: 1.05rem;
}

.canvas-pane {
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: #f3f5f9;
}

.canvas-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.7rem 1rem;
  border-bottom: 1px solid #e2e8f0;
  background: #fff;
}

.toolbar-left,
.toolbar-center,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.viewer-label {
  font-size: 0.88rem;
  font-weight: 700;
  color: #334155;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 0.15rem 0.55rem;
  font-size: 0.75rem;
  font-weight: 700;
  background: #f1f5f9;
  color: #475569;
}

.status-pill.running {
  background: #dbeafe;
  color: #1d4ed8;
}

.status-pill.prd_ready {
  background: #e0e7ff;
  color: #4338ca;
}

.status-pill.spec_approved {
  background: #dcfce7;
  color: #15803d;
}

.status-pill.building {
  background: #fef3c7;
  color: #b45309;
}

.status-pill.completed {
  background: #dcfce7;
  color: #15803d;
}

.status-pill.build_failed {
  background: #fee2e2;
  color: #b91c1c;
}

.status-pill.failed {
  background: #fee2e2;
  color: #b91c1c;
}

.icon-btn {
  width: 2rem;
  height: 2rem;
  border: 1px solid transparent;
  border-radius: 0.55rem;
  background: transparent;
  cursor: pointer;
}

.icon-btn.active,
.icon-btn:hover {
  background: #eff6ff;
  border-color: #bfdbfe;
}

.tool-btn {
  border: 1px solid #e2e8f0;
  border-radius: 0.65rem;
  background: #fff;
  color: #334155;
  font-size: 0.82rem;
  font-weight: 700;
  padding: 0.4rem 0.75rem;
  cursor: pointer;
}

.tool-btn.primary {
  border-color: #2563eb;
  background: #2563eb;
  color: #fff;
}

.tool-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.canvas-body {
  flex: 1;
  overflow: auto;
  padding: 1.25rem;
}

.canvas-body.tablet,
.canvas-body.mobile {
  display: flex;
  justify-content: center;
}

.canvas-body.tablet .prd-panel,
.canvas-body.tablet .canvas-empty {
  width: min(100%, 760px);
}

.canvas-body.mobile .prd-panel,
.canvas-body.mobile .canvas-empty {
  width: min(100%, 420px);
}

.canvas-body.tablet .spec-panel,
.canvas-body.tablet .website-preview-shell {
  width: min(100%, 760px);
}

.canvas-body.mobile .spec-panel,
.canvas-body.mobile .website-preview-shell {
  width: min(100%, 420px);
}

.canvas-empty,
.prd-panel,
.spec-panel {
  min-height: calc(100vh - 5.5rem);
  border: 1px solid #e6ebf2;
  border-radius: 1rem;
  background: #fff;
  box-shadow: 0 12px 30px rgba(15, 23, 42, 0.04);
}

.canvas-empty {
  display: grid;
  place-content: center;
  gap: 0.55rem;
  text-align: center;
  padding: 2rem;
}

.build-spinner {
  width: 2rem;
  height: 2rem;
  margin: 0 auto 0.4rem;
  border: 3px solid #dbeafe;
  border-top-color: #2563eb;
  border-radius: 999px;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.website-preview-shell {
  width: 100%;
  min-height: calc(100vh - 5.5rem);
  overflow: hidden;
  border: 1px solid #dbe2ea;
  border-radius: 1rem;
  background: #fff;
  box-shadow: 0 12px 30px rgba(15, 23, 42, 0.08);
}

.website-frame {
  display: block;
  width: 100%;
  min-height: calc(100vh - 5.5rem);
  border: 0;
  background: #fff;
}

.empty-title {
  margin: 0;
  font-size: 1.15rem;
  font-weight: 750;
}

.empty-desc {
  margin: 0;
  color: #64748b;
  max-width: 28rem;
  line-height: 1.55;
}

.prd-panel {
  padding: 1.25rem 1.35rem 1.75rem;
}

.spec-panel {
  padding: 1.4rem;
}

.spec-summary {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0.75rem;
  margin: 0 0 1.25rem;
}

.spec-summary div,
.section-card,
.spec-list {
  padding: 0.9rem;
  border: 1px solid #e6ebf2;
  border-radius: 0.85rem;
  background: #f8fafc;
}

.spec-summary dt {
  color: #94a3b8;
  font-size: 0.75rem;
  font-weight: 700;
}

.spec-summary dd {
  margin: 0.3rem 0 0;
  color: #334155;
  font-size: 0.88rem;
  line-height: 1.5;
}

.spec-page + .spec-page,
.spec-list {
  margin-top: 1.25rem;
}

.spec-page-head {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  align-items: flex-start;
  margin-bottom: 0.75rem;
}

.spec-page-head h3,
.spec-list h3 {
  margin: 0;
  font-size: 1rem;
}

.spec-page-head p {
  margin: 0.25rem 0 0;
  color: #64748b;
  font-size: 0.84rem;
}

.spec-page-head code {
  padding: 0.2rem 0.45rem;
  border-radius: 0.4rem;
  background: #eff6ff;
  color: #1d4ed8;
}

.section-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.7rem;
}

.section-type {
  color: #2563eb;
  font-size: 0.7rem;
  font-weight: 800;
  text-transform: uppercase;
}

.section-card h4 {
  margin: 0.35rem 0;
  font-size: 0.92rem;
}

.section-card p,
.section-card li,
.spec-list li {
  color: #64748b;
  font-size: 0.82rem;
  line-height: 1.5;
}

.section-card p {
  margin: 0;
}

.section-card ul,
.spec-list ul {
  margin: 0.55rem 0 0;
  padding-left: 1.1rem;
}

.prd-head h2 {
  margin: 0;
  font-size: 1.1rem;
}

.prd-head p {
  margin: 0.35rem 0 1rem;
  color: #94a3b8;
  font-size: 0.85rem;
}

.prd-content {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  font-size: 0.9rem;
  line-height: 1.65;
  color: #334155;
}

@media (max-width: 960px) {
  .workbench {
    grid-template-columns: 1fr;
    grid-template-rows: minmax(42vh, 48vh) 1fr;
    height: auto;
    min-height: 100vh;
  }

  .chat-pane {
    border-right: 0;
    border-bottom: 1px solid #e2e8f0;
    max-height: 48vh;
  }

  .spec-summary,
  .section-grid {
    grid-template-columns: 1fr;
  }
}
</style>
