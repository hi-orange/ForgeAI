<template>
  <main class="workbench" :class="{ 'chat-collapsed': chatCollapsed }">
    <header class="project-bar">
      <div class="project-title">
        <RouterLink to="/" title="返回首页"><ForgeLogo :size="23" /></RouterLink>
        <strong>{{ name }}</strong>
        <button
          class="icon-button"
          type="button"
          :title="chatCollapsed ? '展开对话' : '收起对话'"
          :aria-expanded="!chatCollapsed"
          @click="chatCollapsed = !chatCollapsed"
        >
          <WorkbenchIcon :name="chatCollapsed ? 'chat-expand' : 'chat-collapse'" />
        </button>
      </div>
      <nav class="workspace-tabs" aria-label="工作区">
        <button type="button" :class="{ active: view === 'viewer' }" @click="view = 'viewer'">
          <WorkbenchIcon name="app-viewer" /> 应用查看器
        </button>
        <button type="button" :class="{ active: view === 'plan' }" @click="view = 'plan'">
          <WorkbenchIcon name="overview-grid" /> 计划
        </button>
      </nav>
      <button type="button" class="refresh-button" :disabled="refreshing" @click="refresh">
        <WorkbenchIcon name="refresh" /> 刷新进度
      </button>
    </header>

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
        <article class="agent-message">
          <div class="agent-meta">
            <span class="avatar">F</span><strong>Forge</strong><span>产品助手</span>
          </div>
          <p class="agent-status" role="status">
            <span class="status-dot" :class="{ working: busy || status?.state === 'running' }" />{{
              stateLabel
            }}
          </p>
          <p class="intro">{{ agentText }}</p>
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
              (status?.state === 'ready_for_design'
                ? '计划已批准，点击继续处理。'
                : '上次处理尚未完成，可以继续。')
            }}
          </p>
          <button class="secondary" type="button" :disabled="busy" @click="resume">继续处理</button>
        </div>
        <details v-if="status?.state === 'design_pending'" class="approved-card">
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
          <span>{{ canApprove ? '也可以直接在上方勾选并批准' : '从一个想法开始' }}</span>
          <button
            type="submit"
            class="send-button"
            :disabled="busy || !canWrite || !text.trim()"
            :aria-label="status?.state === 'not_started' ? '开始构建' : '发送补充'"
          >
            ↑
          </button>
        </div>
      </form>
    </aside>

    <section class="canvas-pane" aria-label="应用预览">
      <div class="canvas-toolbar">
        <span>{{ view === 'viewer' ? '预览' : '构建计划' }}</span>
        <div class="preview-address">
          <WorkbenchIcon name="home" /> Home <WorkbenchIcon name="chevron-down" />
        </div>
        <button type="button" :aria-expanded="consoleOpen" @click="consoleOpen = !consoleOpen">
          <WorkbenchIcon name="console" /> 控制台
        </button>
      </div>
      <div v-if="view === 'plan' && status?.app_spec" class="plan-overview">
        <span class="eyebrow">{{ canApprove ? '待批准的建议' : '当前计划' }}</span>
        <h1>{{ planGoal }}</h1>
        <ul>
          <li v-for="item in planItems.filter((entry) => entry.checked)" :key="item.id">
            <WorkbenchIcon name="check" />{{ item.label }}
          </li>
        </ul>
        <p v-if="canApprove">在左侧勾选、编辑或新增需求，批准后继续。</p>
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
        <div class="progress-steps">
          <span class="done">描述想法</span><i /><span
            :class="{ done: canApprove || status?.state === 'design_pending' }"
            >确认计划</span
          ><i /><span>应用预览</span>
        </div>
      </div>
      <aside v-if="consoleOpen" class="console">
        <strong>运行详情</strong>
        <pre>{{ JSON.stringify(status, null, 2) }}</pre>
      </aside>
    </section>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import ForgeLogo from '@/components/ForgeLogo.vue'
import WorkbenchIcon from './components/WorkbenchIcon.vue'
import { useRequirements } from './useRequirements'

const route = useRoute()
const {
  name,
  status,
  messages,
  text,
  error,
  busy,
  refreshing,
  canWrite,
  canResume,
  canApprove,
  planGoal,
  planItems,
  selectedCount,
  addPlanItem,
  needsAcceptance,
  refresh,
  submit,
  resume,
  approve,
} = useRequirements(Number(route.params.id))
const chatCollapsed = ref(false)
const consoleOpen = ref(false)
const editing = ref(false)
const newRequirement = ref('')
const view = ref<'viewer' | 'plan'>('viewer')
const thread = ref<HTMLElement | null>(null)
const featureCount = computed(
  () => planItems.value.filter((item) => item.kind === 'feature').length,
)
const labels = {
  not_started: '准备开始',
  pending: '等待处理',
  running: '正在整理构建计划…',
  retry_available: '处理已暂停',
  stopped: '当前流程已停止',
  needs_user_input: '补充一个想法',
  awaiting_approval: '构建计划已准备好',
  ready_for_design: '计划已批准',
  design_pending: '计划已批准，等待后续处理',
}
function kindLabel(kind: string) {
  return (
    ({ data: '数据', interface: '界面', constraint: '约束' } as Record<string, string>)[kind] ??
    kind
  )
}
const stateLabel = computed(() =>
  busy.value ? '正在处理…' : status.value ? labels[status.value.state] : '正在加载项目…',
)
const agentText = computed(() => {
  if (canApprove.value) return '我整理了一份初步计划。选择你想要的功能，随时补充自己的想法。'
  if (status.value?.state === 'design_pending') return '你的选择已保存，后续工作将按这份计划进行。'
  if (busy.value || status.value?.state === 'running')
    return '我正在把你的想法整理成可选择的功能和页面。'
  return '告诉我你想做什么，我会先整理一份简洁的构建计划。'
})
const previewTitle = computed(() =>
  status.value?.state === 'design_pending'
    ? '构建计划已确认'
    : canApprove.value
      ? '你的应用，即将从这里开始'
      : '把想法变成看得见的应用',
)
const previewDescription = computed(() =>
  status.value?.state === 'design_pending'
    ? '需求已交给工程交付任务。当前版本尚未接入代码生成，暂时没有可预览的应用。'
    : canApprove.value
      ? '在左侧选择需要的功能，编辑计划或补充需求，然后批准构建。'
      : '描述你的想法，确认核心功能，应用生成后将在这里预览。',
)
function addRequirement() {
  if (!newRequirement.value.trim() || featureCount.value >= 50) return
  addPlanItem(newRequirement.value)
  newRequirement.value = ''
}
watch(
  () => [messages.value.length, status.value?.state],
  async () => {
    await nextTick()
    thread.value?.scrollTo({ top: thread.value.scrollHeight, behavior: 'smooth' })
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
  margin: 0 0 26px 39px;
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
.send-button {
  border: 0;
  border-radius: 50%;
  background: var(--accent);
  color: white;
  width: 32px;
  height: 32px;
  font-size: 23px;
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
.canvas-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  min-height: 34px;
  padding: 0 14px;
  background: #fafafa;
  border-bottom: 1px solid #f1f1f3;
  color: #777;
  font-size: 11px;
  button {
    display: flex;
    gap: 4px;
    align-items: center;
    border: 0;
    background: transparent;
    color: #555;
  }
}
.preview-address {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 60px;
  padding: 3px 10px;
  border: 1px solid #eaeaec;
  border-radius: 20px;
}
.preview-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 30px;
  text-align: center;
  h1 {
    font-size: clamp(20px, 2vw, 28px);
    letter-spacing: -0.6px;
    font-weight: 500;
    margin: 12px 0;
  }
  > p {
    max-width: 390px;
    color: #9193a1;
    line-height: 1.9;
    margin: 0;
  }
}
.eyebrow {
  font-size: 11px;
  color: #8b8fbd;
  letter-spacing: 2px;
}
.preview-illustration {
  display: flex;
  position: relative;
  width: 246px;
  height: 150px;
  padding: 12px;
  gap: 9px;
  margin-bottom: 34px;
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
  background: white;
  border-radius: 7px;
}
.mini-nav {
  display: flex;
  justify-content: space-between;
  i {
    width: 23px;
    height: 4px;
    background: #e3e7f5;
    border-radius: 4px;
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
  right: -14px;
  top: -18px;
  font-size: 36px;
  color: #9ba8ff;
}
.progress-steps {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 30px;
  font-size: 11px;
  color: #c3c4cf;
  i {
    width: 25px;
    height: 1px;
    background: #e5e6ed;
  }
  .done {
    color: #7d88bc;
  }
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
    list-style: none;
    padding: 0;
  }
  li {
    display: flex;
    gap: 12px;
    align-items: baseline;
    line-height: 1.8;
    padding: 15px 0;
    border-bottom: 1px solid #eee;
  }
  p {
    color: #8a8d9e;
  }
}
.console {
  border-top: 1px solid #ececf1;
  padding: 14px;
  max-height: 180px;
  overflow: auto;
  font-size: 11px;
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: #7a7d8d;
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
  .preview-address {
    gap: 12px;
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
