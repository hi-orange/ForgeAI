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

          <div v-if="showPlanCard" class="plan-card">
            <p class="plan-intro">
              请从这些核心功能和页面设计中，选择您希望优先实现或进一步讨论的部分。
            </p>
            <ul class="plan-list">
              <li v-for="item in planItems" :key="item.id">
                <label>
                  <input v-model="item.checked" type="checkbox" :disabled="planApproved" />
                  <span>{{ item.label }}</span>
                </label>
              </li>
            </ul>
            <div class="plan-actions">
              <button type="button" class="btn ghost" :disabled="planApproved" @click="resetPlan">
                调整计划
              </button>
              <button
                type="button"
                class="btn primary"
                :disabled="planApproved || !selectedCount"
                @click="approvePlan"
              >
                {{ planApproved ? '已批准' : '批准' }}
              </button>
            </div>
            <p v-if="planApproved" class="plan-done">计划已批准，后续 Agent 流水线将基于所选范围继续。</p>
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
          <button type="button" class="tool-btn">分享</button>
          <button type="button" class="tool-btn primary">发布</button>
        </div>
      </header>

      <div class="canvas-body" :class="previewMode">
        <div v-if="starting || project?.status === 'running'" class="canvas-empty">
          <p class="empty-title">Product Manager 正在撰写 PRD…</p>
          <p class="empty-desc">左侧可查看进度，完成后将展示需求文档与计划确认。</p>
        </div>
        <div v-else-if="project?.status === 'failed'" class="canvas-empty">
          <p class="empty-title">生成失败</p>
          <p class="empty-desc">{{ projects.error || '请返回首页重试，或在左侧继续补充需求。' }}</p>
          <button type="button" class="btn primary" @click="retryStart">重新生成</button>
        </div>
        <div v-else-if="project?.prd" class="prd-panel">
          <div class="prd-head">
            <h2>产品需求文档 PRD</h2>
            <p>由 Product Manager Agent 生成</p>
          </div>
          <pre class="prd-content">{{ project.prd }}</pre>
        </div>
        <div v-else class="canvas-empty">
          <p class="empty-title">等待开始</p>
          <p class="empty-desc">提交需求后，这里会展示 PRD 与后续预览。</p>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import ForgeLogo from '@/components/ForgeLogo.vue'
import { useProjectStore } from '@/stores/project'

type PlanItem = { id: string; label: string; checked: boolean }
type PreviewMode = 'desktop' | 'tablet' | 'mobile'

const route = useRoute()
const projects = useProjectStore()

const bootLoading = ref(true)
const bootError = ref<string | null>(null)
const followUp = ref('')
const planApproved = ref(false)
const planItems = reactive<PlanItem[]>([])
const previewMode = ref<PreviewMode>('desktop')
const threadRef = ref<HTMLElement | null>(null)
const starting = computed(() => projects.starting)

const project = computed(() => projects.current)
const workflowId = computed(() => projects.workflowId)

const previewModes: { key: PreviewMode; label: string; icon: string }[] = [
  { key: 'desktop', label: '桌面', icon: '🖥' },
  { key: 'tablet', label: '平板', icon: '▤' },
  { key: 'mobile', label: '手机', icon: '▢' },
]

const statusLabel = computed(() => {
  const map: Record<string, string> = {
    draft: '草稿',
    running: '构建中',
    prd_ready: 'PRD 已完成',
    completed: '已完成',
    failed: '失败',
  }
  return map[project.value?.status || ''] || project.value?.status || '—'
})

const stepLabel = computed(() => {
  if (starting.value || project.value?.status === 'running') return '正在处理第 1 步'
  if (project.value?.status === 'prd_ready') return '已处理 1 步'
  if (project.value?.status === 'failed') return '第 1 步失败'
  return '等待开始'
})

const agentStatusText = computed(() => {
  if (starting.value || project.value?.status === 'running') {
    return '正在理解需求并撰写 PRD，请稍候…'
  }
  if (project.value?.status === 'failed') {
    return projects.error || 'PRD 生成失败，可以点击右侧重新生成。'
  }
  if (project.value?.prd) {
    return planApproved.value
      ? '计划已确认。右侧可查看完整 PRD。'
      : 'PRD 已生成。请确认左侧计划范围后继续。'
  }
  return '准备开始需求分析。'
})

const showPlanCard = computed(
  () => Boolean(project.value?.prd) && project.value?.status === 'prd_ready',
)

const selectedCount = computed(() => planItems.filter((item) => item.checked).length)

function extractPlanItems(prd: string): PlanItem[] {
  const fromTree = [...prd.matchAll(/^[ \t]*[│├└].*?[├└]──\s*(.+)$/gm)]
    .map((m) => m[1]?.trim())
    .filter((label): label is string => Boolean(label))
    .filter((label) => label.length >= 2 && label.length <= 40)

  const unique = [...new Set(fromTree)].slice(0, 8)
  if (unique.length >= 3) {
    return unique.map((label, index) => ({
      id: `f-${index}`,
      label,
      checked: true,
    }))
  }

  return [
    { id: 'home', label: '首页 / 落地页', checked: true },
    { id: 'detail', label: '内容详情页', checked: true },
    { id: 'about', label: '关于 / 介绍页', checked: true },
    { id: 'nav', label: '导航与整体布局', checked: true },
    { id: 'list', label: '列表 / 分类筛选', checked: true },
  ]
}

function rebuildPlan(prd: string | null | undefined) {
  planItems.splice(0, planItems.length, ...extractPlanItems(prd || ''))
  planApproved.value = false
}

function resetPlan() {
  planItems.forEach((item) => {
    item.checked = true
  })
  planApproved.value = false
}

function approvePlan() {
  if (!selectedCount.value) return
  planApproved.value = true
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
  followUpNotes.value = []
  planItems.splice(0, planItems.length)
  projects.workflowId = null

  try {
    await projects.fetchOne(id)
    if (Number(route.params.id) !== id) return
    bootLoading.value = false
    await scrollThread()
    await ensureStarted(id)
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

.canvas-empty,
.prd-panel {
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
}
</style>
