<template>
  <main class="workbench" :class="{ 'chat-collapsed': chatCollapsed }">
    <ProjectTopbar
      :name="name"
      :workspace-view="workspaceView"
      :chat-collapsed="chatCollapsed"
      :history-open="historyOpen"
      :mode-tabs="modeTabs"
      @update:workspace-view="setWorkspaceView"
      @toggle-chat="chatCollapsed = !chatCollapsed"
      @toggle-history="historyOpen = !historyOpen"
      @share="shareProject"
      @publish="publishProject"
    >
      <template #brand>
        <RouterLink to="/" title="返回首页"><ForgeLogo :size="22" /></RouterLink>
        <strong>{{ name }}</strong>
      </template>
    </ProjectTopbar>

    <aside v-show="!chatCollapsed" class="chat-pane" aria-label="项目对话">
      <div ref="thread" class="chat-thread">
        <article
          v-for="message in messages"
          :key="message.id"
          class="message"
          :class="message.sender"
        >
          <div v-if="message.sender !== 'user'" class="agent-meta">
            <span class="avatar">F</span> Forge
          </div>
          <p>{{ message.content }}</p>
        </article>
        <article v-if="!awaitingIdeaAfterReply" class="agent-message">
          <div class="agent-meta">
            <span class="avatar">F</span><strong>Forge</strong
            ><span>{{ postApproval ? '工程师' : '产品助手' }}</span>
          </div>
          <p class="agent-status" role="status">
            <span
              class="status-dot"
              :class="{
                working:
                  busy ||
                  status?.state === 'running' ||
                  status?.state === 'design_running' ||
                  (status?.state === 'engineering_running' && !status.error),
              }"
            />{{ stateLabel }}
          </p>
          <p v-if="agentText" class="intro">{{ agentText }}</p>
          <BuildTimeline
            v-if="status?.activities?.length"
            :activities="status.activities"
            :running="engineeringRunning"
            @open-file="openWorkspaceFile"
          />
        </article>

        <section v-if="canApprove" class="approval-card" aria-labelledby="plan-heading">
          <div class="plan-heading">
            <h2 id="plan-heading">确认构建计划</h2>
            <span>{{ selectedCount }} 项已选</span>
          </div>
          <p class="plan-hint">勾选本次要实现的内容，也可以编辑或新增。</p>
          <label v-if="editing" class="goal-label"
            >应用目标<textarea v-model="planGoal" rows="2" maxlength="2000" :disabled="busy" />
          </label>
          <p v-else class="plan-goal">{{ planGoal }}</p>
          <div class="plan-list">
            <div
              v-for="(item, index) in planItems"
              :key="item.id"
              class="plan-row"
              :class="{ unchecked: !item.checked }"
            >
              <span class="item-number">{{ index + 1 }}.</span>
              <textarea
                v-if="editing"
                v-model="item.label"
                rows="2"
                maxlength="2000"
                :aria-label="'编辑第 ' + (index + 1) + ' 项需求'"
                :disabled="busy"
              />
              <label v-else :for="'plan-check-' + item.id"
                ><small v-if="item.kind !== 'feature'">{{ kindLabel(item.kind) }} · </small
                >{{ item.label }}</label
              >
              <input
                :id="'plan-check-' + item.id"
                v-model="item.checked"
                type="checkbox"
                :disabled="busy"
                :aria-label="'选择第 ' + (index + 1) + ' 项：' + item.label"
              />
              <label v-if="needsAcceptance(item)" class="acceptance-label">
                怎样算完成
                <textarea
                  v-model="item.acceptance"
                  rows="2"
                  maxlength="2000"
                  :disabled="busy"
                  :aria-label="'第 ' + (index + 1) + ' 项功能的验收条件'"
                  placeholder="写下操作和预期结果，例如：游客打开首页，无需登录即可看到公开列表。"
                />
              </label>
            </div>
            <p v-if="!planItems.length" class="plan-hint">添加第一项功能，即可批准计划。</p>
          </div>
          <form class="add-requirement" @submit.prevent="addRequirement">
            <input
              v-model="newRequirement"
              aria-label="新增需求"
              placeholder="＋ 新增一项需求…"
              maxlength="2000"
              :disabled="busy"
            />
            <button type="submit" :disabled="busy || !newRequirement.trim() || featureCount >= 50">
              添加
            </button>
          </form>
          <div class="plan-actions">
            <button type="button" class="secondary" :disabled="busy" @click="editing = !editing">
              {{ editing ? '完成编辑' : '编辑计划' }}
            </button>
            <button
              type="button"
              class="primary"
              :disabled="busy || !selectedCount || !planGoal.trim()"
              @click="approve"
            >
              {{ busy ? '处理中…' : '批准并构建' }}
            </button>
          </div>
        </section>
        <div
          v-if="status?.state === 'needs_user_input' && !status.app_spec?.features.length"
          class="clarification"
        >
          <p v-for="question in status.result?.open_questions.slice(0, 1)" :key="question">
            {{ question }}
          </p>
        </div>
        <div v-if="canResume" class="resume-card">
          <p>
            {{
              status?.error ||
              (status?.state === 'engineering_running'
                ? '构建似乎还没有开始写入文件，可以继续。'
                : status?.state === 'ready_for_design'
                  ? '计划已批准，点击继续处理。'
                  : '上次处理尚未完成，可以继续。')
            }}
          </p>
          <button class="secondary" type="button" :disabled="busy" @click="resume">继续处理</button>
        </div>
        <details
          v-if="
            status?.state === 'design_pending' ||
            status?.state === 'design_running' ||
            status?.state === 'engineering_running' ||
            status?.state === 'engineering_generated'
          "
          class="approved-card"
        >
          <summary>
            <WorkbenchIcon name="check" /> 计划已批准 ·
            {{ status.app_spec?.features.length }} 项功能
          </summary>
          <ul>
            <li v-for="feature in status.app_spec?.features" :key="feature.id">
              {{ feature.text }}
            </li>
          </ul>
        </details>
        <p v-if="error" class="error" role="alert">{{ error }}</p>
      </div>
      <form class="composer" @submit.prevent="submit">
        <textarea
          v-model="text"
          rows="3"
          maxlength="8000"
          aria-label="描述应用或补充需求"
          :disabled="busy || !canWrite"
          :placeholder="canApprove ? '告诉我还想怎样调整计划…' : '描述你想构建的应用…'"
          @keydown.enter.exact.prevent="!$event.isComposing && submit()"
        />
        <div class="composer-bottom">
          <span>{{ composerHint }}</span>
          <div class="composer-actions">
            <button
              v-if="canPause || pausing"
              type="button"
              class="send-button pause-button"
              :disabled="pausing"
              :aria-label="pausing ? '正在暂停构建' : '暂停构建'"
              :title="pausing ? '正在暂停构建' : '暂停构建'"
              @click="pause"
            >
              <span class="pause-icon" aria-hidden="true" />
            </button>
            <button
              v-else
              type="submit"
              class="send-button"
              :disabled="busy || !canWrite || !text.trim()"
              :aria-label="status?.state === 'not_started' ? '开始构建' : '发送补充'"
            >
              ↑
            </button>
          </div>
        </div>
      </form>
    </aside>

    <section class="canvas-pane" :aria-label="canvasLabel">
      <WorkspaceEditorPane
        v-if="workspaceView === 'editor'"
        :project-id="projectId"
        :ready="Boolean(status?.workspace_ready)"
        :run-id="status?.run_id"
        :generation="workspaceGeneration"
        :written-path="latestWrittenPath"
        :requested-path="requestedWorkspacePath"
        :request-sequence="workspaceRequestSequence"
        @download="downloadWorkspaceHint"
      />
      <AppPreviewPane
        v-else-if="workspaceView === 'design'"
        :status="status"
        :plan-goal="planGoal"
        :plan-items="planItems"
        :can-approve="canApprove"
        :can-resume="canResume"
        :busy="busy"
        :refreshing="refreshing"
        @refresh="refresh"
        @resolve="resume"
      />
      <div v-else class="mode-placeholder">
        <h2>{{ placeholderTitle }}</h2>
        <p>{{ placeholderDescription }}</p>
      </div>
    </section>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import ForgeLogo from '@/components/ForgeLogo.vue'
import type { WorkspaceView } from './projectView'
import { isNearThreadBottom } from './buildTimeline'
import AppPreviewPane from './components/AppPreviewPane.vue'
import BuildTimeline from './components/BuildTimeline.vue'
import ProjectTopbar, { type TopbarModeTab } from './components/ProjectTopbar.vue'
import WorkbenchIcon from './components/WorkbenchIcon.vue'
import WorkspaceEditorPane from './components/WorkspaceEditorPane.vue'
import { useRequirements } from './useRequirements'

const route = useRoute()
const projectId = Number(route.params.id)
const {
  name,
  status,
  messages,
  text,
  error,
  busy,
  pausing,
  refreshing,
  canWrite,
  canResume,
  canPause,
  canApprove,
  planGoal,
  planItems,
  selectedCount,
  addPlanItem,
  needsAcceptance,
  refresh,
  submit,
  resume,
  pause,
  approve,
} = useRequirements(projectId)
const chatCollapsed = ref(false)
const editing = ref(false)
const newRequirement = ref('')
const historyOpen = ref(false)
const workspaceView = ref<WorkspaceView>('design')
const requestedWorkspacePath = ref<string | null>(null)
const workspaceRequestSequence = ref(0)
const thread = ref<HTMLElement | null>(null)
let autoOpenedWorkspace = false
const featureCount = computed(
  () => planItems.value.filter((item) => item.kind === 'feature').length,
)
const modeTabs: TopbarModeTab[] = [
  { id: 'design', icon: 'desktop', label: '预览' },
  { id: 'editor', icon: 'editor-code', label: '编辑器' },
  { id: 'cloud', icon: 'cloud-upload', label: '云' },
  { id: 'files', icon: 'folder', label: '文件' },
  { id: 'more', icon: 'more-dots', label: '更多' },
]
const labels = {
  not_started: '准备开始',
  pending: '等待处理',
  running: '正在整理构建计划…',
  retry_available: '处理已暂停',
  stopped: '当前流程已停止',
  needs_user_input: '补充一个想法',
  awaiting_approval: '构建计划已准备好',
  ready_for_design: '计划已批准',
  design_pending: '计划已批准，正在开始构建',
  design_running: 'Architect 正在设计系统',
  engineering_running: '正在根据获批需求编写代码',
  engineering_generated: '业务代码已写入工作区',
}
function kindLabel(kind: string) {
  return (
    ({ data: '数据', interface: '界面', constraint: '约束' } as Record<string, string>)[kind] ??
    kind
  )
}
const postApproval = computed(
  () =>
    status.value?.state === 'design_pending' ||
    status.value?.state === 'design_running' ||
    status.value?.state === 'engineering_running' ||
    status.value?.state === 'engineering_generated',
)
const engineeringRunning = computed(
  () => status.value?.state === 'engineering_running' && !status.value.error,
)
const successfulWrites = computed(
  () => status.value?.activities?.filter((step) => step.name === 'apply_patch' && step.ok) ?? [],
)
const workspaceGeneration = computed(() => successfulWrites.value.length)
const latestWrittenPath = computed(() => successfulWrites.value.at(-1)?.detail || null)
// 问询等已在对话里回过话时，不再用常驻卡重复「告诉我你想做什么」。
const awaitingIdeaAfterReply = computed(() => {
  if (busy.value || status.value?.state !== 'not_started') return false
  return messages.value.at(-1)?.sender === 'assistant'
})
const understandingFirstTurn = computed(
  () =>
    busy.value &&
    (status.value?.state === 'not_started' || !status.value) &&
    messages.value.some((message) => message.sender === 'user'),
)
const stateLabel = computed(() => {
  if (understandingFirstTurn.value) return '正在理解…'
  if (busy.value) return '正在处理…'
  if (awaitingIdeaAfterReply.value) return '等待你的描述'
  return status.value ? labels[status.value.state] : '正在加载项目…'
})
const agentText = computed(() => {
  if (awaitingIdeaAfterReply.value) return ''
  if (understandingFirstTurn.value) return '我在理解你刚才说的话。'
  if (canApprove.value) return '我整理了一份初步计划。选择你想要的功能，随时补充自己的想法。'
  if (postApproval.value) {
    if (status.value?.state === 'engineering_generated') {
      return '已按获批需求改写工作区源码。可在「编辑器」查看；正式验收与预览仍未标记为完成。'
    }
    if (status.value?.state === 'engineering_running') {
      return status.value.activities?.length
        ? '正在按获批需求读写工作区。当前动作保持精简，完整历史可按需展开。'
        : '正在根据获批需求构建应用，右侧会同步显示真实文件。'
    }
    if (status.value?.state === 'design_running') {
      return 'Architect 正在把获批 PRD 转化为模块、接口和数据结构设计。'
    }
    return '计划已批准，正在开始构建应用。'
  }
  if (busy.value || status.value?.state === 'running')
    return '我正在把你的想法整理成可选择的功能和页面。'
  return '告诉我你想做什么，我会先整理一份简洁的构建计划。'
})
const composerHint = computed(() => {
  if (pausing.value) return '正在暂停，当前进度会被保留'
  if (canPause.value) return '构建运行中，点击右侧按钮可暂停'
  return canApprove.value ? '也可以直接在上方勾选并批准' : '从一个想法开始'
})
const canvasLabel = computed(() => {
  if (workspaceView.value === 'editor') return '代码编辑器'
  if (workspaceView.value === 'design') return '设计预览'
  return '工作区'
})
const placeholderTitle = computed(() => {
  if (workspaceView.value === 'cloud') return 'Atoms 云'
  if (workspaceView.value === 'files') return '文件与资源'
  return '更多工具'
})
const placeholderDescription = computed(() => {
  if (workspaceView.value === 'cloud') return '应用数据库、用户与环境管理将在后续接入。'
  if (workspaceView.value === 'files') return '资源库与上传能力尚未接入；源码请使用「编辑器」。'
  return 'Terminal 等工具入口将放在这里。'
})

function setWorkspaceView(view: WorkspaceView) {
  workspaceView.value = view
}
function openWorkspaceFile(path: string) {
  workspaceView.value = 'editor'
  requestedWorkspacePath.value = path
  workspaceRequestSequence.value += 1
}
function shareProject() {
  void navigator.clipboard?.writeText(window.location.href)
}
function publishProject() {
  window.alert('发布尚未接入。当前仅支持工作区源码浏览。')
}
function downloadWorkspaceHint() {
  window.alert(
    status.value?.workspace_path
      ? `工作区位于：\n${status.value.workspace_path}\n\n打包下载接口尚未接入。`
      : '工作区尚未准备好。',
  )
}
function addRequirement() {
  if (!newRequirement.value.trim() || featureCount.value >= 50) return
  addPlanItem(newRequirement.value)
  newRequirement.value = ''
}
watch(
  () => [status.value?.state, status.value?.workspace_ready] as const,
  ([state, workspaceReady]) => {
    if (
      !autoOpenedWorkspace &&
      workspaceReady &&
      (state === 'design_pending' ||
        state === 'design_running' ||
        state === 'engineering_running' ||
        state === 'engineering_generated')
    ) {
      autoOpenedWorkspace = true
      workspaceView.value = 'editor'
    }
  },
  { immediate: true },
)
watch(
  () => [messages.value.length, status.value?.state, status.value?.activities?.length],
  async () => {
    const shouldFollow = thread.value ? isNearThreadBottom(thread.value) : true
    await nextTick()
    if (shouldFollow) thread.value?.scrollTo({ top: thread.value.scrollHeight, behavior: 'smooth' })
  },
)
</script>

<style scoped lang="scss">
.plan-row {
  flex-wrap: wrap;
}
.plan-row .acceptance-label {
  flex: 1 0 calc(100% - 24px);
  margin-left: 24px;
  display: grid;
  gap: 6px;
  font-size: 12px;
  color: #59617e;
  textarea {
    width: 100%;
    box-sizing: border-box;
  }
}
.workbench {
  --accent: #4c63ff;
  display: grid;
  grid-template-columns: clamp(360px, 28vw, 500px) minmax(0, 1fr);
  grid-template-rows: 42px minmax(0, 1fr);
  gap: 8px;
  height: 100dvh;
  padding: 6px 8px 8px;
  box-sizing: border-box;
  background: #f6f6f6;
  color: #26272d;
  font-size: 13px;
}
.workbench.chat-collapsed {
  grid-template-columns: 1fr;
}
button,
input,
textarea {
  font: inherit;
}
button {
  cursor: pointer;
}
button:disabled {
  opacity: 0.45;
  cursor: default;
}
button:focus-visible,
input:focus-visible,
textarea:focus-visible,
summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}
.project-bar {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: clamp(360px, 28vw, 500px) 1fr auto;
  align-items: center;
  gap: 8px;
}
.project-title {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 8px;
  min-width: 0;
  strong {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 500;
  }
  a {
    display: flex;
    color: inherit;
  }
}
.icon-button {
  display: grid;
  place-items: center;
  border: 0;
  background: transparent;
  width: 30px;
  height: 30px;
  margin-left: auto;
  color: #888;
}
.workspace-tabs {
  display: flex;
  gap: 5px;
  button {
    display: flex;
    align-items: center;
    gap: 7px;
    border: 0;
    padding: 7px 12px;
    border-radius: 20px;
    background: transparent;
    color: #787981;
  }
  .active {
    background: #eaeaea;
    color: #25252a;
  }
}
.refresh-button {
  display: flex;
  align-items: center;
  gap: 5px;
  border: 1px solid #e7e7eb;
  border-radius: 20px;
  padding: 6px 12px;
  background: transparent;
  color: #74757e;
}
.chat-pane {
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
}
.chat-thread {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 28px 10px 14px;
  scrollbar-width: thin;
}
.message {
  margin: 0 0 28px;
  line-height: 1.8;
  p {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
}
.message.user {
  margin-left: auto;
  width: fit-content;
  max-width: 85%;
  padding: 10px 14px;
  border-radius: 16px 4px 16px 16px;
  background: #ededee;
}
.agent-meta {
  display: flex;
  align-items: center;
  gap: 9px;
  color: #8d8e99;
  font-size: 12px;
  strong {
    color: #51546b;
    font-weight: 500;
  }
}
.avatar {
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: linear-gradient(140deg, #bfe5ff, #d0d7ff);
  color: #475be2;
  font-weight: 700;
}
.agent-status {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 18px 0 10px 39px;
  color: #808292;
  font-size: 12px;
}
.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #abb3c6;
  &.working {
    background: var(--accent);
    animation: pulse 1.5s infinite;
  }
}
.intro {
  margin: 0 0 12px 39px;
  line-height: 1.9;
  color: #787b8b;
}
.approval-card {
  padding: 16px 8px 8px;
  border-radius: 22px;
  background: #e2e5f8;
}
.plan-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 8px;
  h2 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
  }
  span {
    font-size: 11px;
    color: #626b93;
  }
}
.plan-hint,
.plan-goal {
  margin: 8px;
  color: #59617e;
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.plan-goal {
  color: #323b5f;
}
.plan-list {
  border-radius: 17px;
  padding: 5px 12px;
  background: #fff;
  margin-top: 12px;
}
.plan-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 13px 0;
  line-height: 1.75;
  label {
    flex: 1;
    cursor: pointer;
    overflow-wrap: anywhere;
  }
  small {
    color: #737e9c;
  }
  input {
    margin-top: 5px;
    accent-color: var(--accent);
    flex: 0 0 auto;
    width: 15px;
    height: 15px;
  }
  textarea {
    flex: 1;
    min-width: 0;
    border: 1px solid #dce0f3;
    border-radius: 6px;
    padding: 5px;
    resize: vertical;
  }
  &.unchecked label {
    color: #a0a3af;
  }
}
.item-number {
  color: #768097;
}
.goal-label {
  display: block;
  padding: 8px;
  color: #626b93;
  textarea {
    display: block;
    box-sizing: border-box;
    width: 100%;
    margin-top: 5px;
    padding: 8px;
    border: 1px solid #c9cfe9;
    border-radius: 8px;
    resize: vertical;
  }
}
.add-requirement {
  display: flex;
  gap: 6px;
  padding: 8px 4px;
  input {
    min-width: 0;
    flex: 1;
    padding: 8px;
    border: 0;
    background: transparent;
    color: #364069;
  }
  button {
    border: 0;
    color: var(--accent);
    background: transparent;
    padding: 5px 8px;
  }
}
.plan-actions {
  display: flex;
  gap: 8px;
  button {
    flex: 1;
  }
}
.primary,
.secondary {
  border: 1px solid var(--accent);
  border-radius: 20px;
  padding: 8px 14px;
  background: var(--accent);
  color: white;
}
.secondary {
  background: transparent;
  color: var(--accent);
}
.resume-card,
.clarification,
.approved-card {
  padding: 14px;
  border: 1px solid #e0e3ee;
  border-radius: 14px;
  margin: 14px 0;
  line-height: 1.8;
}
.approved-card summary {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  color: #4c6285;
}
.error {
  padding: 12px;
  border-radius: 10px;
  background: #fff0ef;
  color: #b44f4a;
  overflow-wrap: anywhere;
}
.composer {
  flex-shrink: 0;
  padding: 14px 12px 10px;
  margin-top: 8px;
  border-radius: 24px;
  background: white;
  border: 1px solid #efeff0;
  textarea {
    box-sizing: border-box;
    width: 100%;
    resize: none;
    border: 0;
    padding: 0;
    background: transparent;
    color: #333;
    line-height: 1.7;
    outline-offset: 4px;
  }
}
.composer-bottom {
  display: flex;
  justify-content: space-between;
  align-items: center;
  color: #aaadbb;
  font-size: 11px;
  margin-top: 9px;
}
.composer-actions {
  display: flex;
  align-items: center;
}
.send-button {
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 50%;
  background: var(--accent);
  color: white;
  width: 32px;
  height: 32px;
  font-size: 23px;
}
.pause-button {
  background: #252936;
}
.pause-icon {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  background: currentColor;
}
.canvas-pane {
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  border: 1px solid #e8e8eb;
  border-radius: 18px;
  overflow: hidden;
  background: white;
}
.mode-placeholder {
  flex: 1;
  display: grid;
  place-content: center;
  gap: 8px;
  padding: 32px;
  text-align: center;
  color: #71717a;
  h2 {
    margin: 0;
    color: #27272a;
    font-size: 1.15rem;
    font-weight: 600;
  }
  p {
    margin: 0;
    max-width: 28rem;
    line-height: 1.7;
  }
}
@keyframes pulse {
  50% {
    opacity: 0.35;
  }
}
@media (prefers-reduced-motion: reduce) {
  .status-dot.working {
    animation: none;
  }
}
@media (max-width: 850px) {
  .workbench {
    grid-template-columns: minmax(300px, 42%) 1fr;
  }
  .project-bar {
    grid-template-columns: minmax(0, 1fr) auto;
  }
  .workspace-tabs {
    display: none;
  }
}
@media (max-width: 640px) {
  .workbench {
    grid-template-columns: 1fr;
  }
  .canvas-pane {
    display: none;
  }
  .chat-collapsed .canvas-pane {
    display: flex;
  }
  .chat-thread {
    padding-top: 15px;
  }
  .refresh-button {
    font-size: 11px;
  }
}
</style>
