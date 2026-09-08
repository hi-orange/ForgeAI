<template>
  <main class="requirements-workbench">
    <header>
      <RouterLink to="/">← 返回首页</RouterLink>
      <h1>{{ name }}</h1>
      <p role="status">{{ busy ? '正在处理，请稍候…' : stateLabel }}</p>
      <button type="button" :disabled="refreshing" @click="refresh">刷新进度</button>
    </header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div class="columns">
      <section aria-labelledby="conversation-heading">
        <h2 id="conversation-heading">一起确认需求</h2>
        <p class="hint">ProductManager 会整理你的需求，有不清楚的地方会继续提问。</p>
        <article v-for="message in messages" :key="message.id" class="message">
          <small>{{ message.sender === 'user' ? '你' : message.sender }}</small>
          <p>{{ message.content }}</p>
        </article>
        <article v-if="status?.result?.open_questions.length" class="questions">
          <h3>还需要你确认</h3>
          <ol>
            <li v-for="question in status.result.open_questions" :key="question">{{ question }}</li>
          </ol>
        </article>
        <form v-if="canWrite" @submit.prevent="submit">
          <label for="requirements-answer">{{
            status?.state === 'needs_user_input' ? '补充回答' : '应用需求'
          }}</label>
          <textarea
            id="requirements-answer"
            v-model="text"
            :disabled="busy"
            rows="5"
            maxlength="8000"
            required
            :placeholder="
              status?.state === 'needs_user_input' ? '按上面的问题补充说明…' : '你想做什么应用？'
            "
          />
          <button type="submit" :disabled="busy || !text.trim()">
            {{ status?.state === 'needs_user_input' ? '提交补充回答' : '开始整理需求' }}
          </button>
        </form>
        <div v-if="canResume">
          <p>
            {{
              status?.state === 'ready_for_design'
                ? '需求已保存，设计任务尚未创建。继续派工不会重新整理需求。'
                : status?.error || '任务尚未完成，可以继续处理。恢复会使用原来的需求输入。'
            }}
          </p>
          <button type="button" :disabled="busy" @click="resume">
            {{
              status?.state === 'ready_for_design'
                ? '继续派给架构师'
                : status?.state === 'pending'
                  ? '继续处理'
                  : '恢复执行'
            }}
          </button>
        </div>
        <p v-if="status?.state === 'running'" class="hint">
          正在整理需求。刷新页面后仍能查看进度，请不要重复提交。
        </p>
        <div v-if="status?.state === 'design_pending'" class="notice">
          <p>已派给架构师（SolutionArchitect），等待处理。当前仅创建任务，尚未生成设计或代码。</p>
          <p>设计任务：{{ status.result?.design_task_id }}</p>
          <p>使用的需求版本编号：{{ status.result?.configuration_item_id }}</p>
        </div>
        <p v-if="status?.state === 'stopped'" class="hint">
          当前构建已结束或不在需求阶段，不能从这里继续。
        </p>
      </section>
      <section aria-labelledby="spec-heading">
        <h2 id="spec-heading">当前需求文档</h2>
        <template v-if="status?.app_spec">
          <h3>要解决什么问题</h3>
          <p>{{ status.app_spec.goal }}</p>
          <div v-for="section in specSections" :key="section.key">
            <h3>{{ section.label }}</h3>
            <ul>
              <li v-for="(item, index) in status.app_spec[section.key]" :key="index">{{ item }}</li>
            </ul>
            <p v-if="!status.app_spec[section.key].length" class="hint">暂无</p>
          </div>
          <p class="hint">每轮回答都会生成一份新需求，之前的版本仍然保留。</p>
        </template>
        <p v-else class="hint">整理完成后，这里会显示经过校验并保存的需求内容。</p>
      </section>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
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
  refresh,
  submit,
  resume,
} = useRequirements(Number(route.params.id))
const labels = {
  not_started: '等待开始整理需求',
  pending: '等待处理',
  running: '正在整理需求',
  retry_available: '处理未完成，可以恢复',
  stopped: '当前不能继续',
  needs_user_input: '等待你补充回答',
  ready_for_design: '需求已就绪，等待派工',
  design_pending: '设计任务已创建，等待处理',
}
const stateLabel = computed(() => (status.value ? labels[status.value.state] : '正在读取进度…'))
const specSections = [
  { key: 'target_users', label: '谁来使用' },
  { key: 'features', label: '需要哪些功能' },
  { key: 'data_requirements', label: '需要保存哪些数据' },
  { key: 'interface_requirements', label: '页面和交互' },
  { key: 'constraints', label: '限制和边界' },
  { key: 'acceptance_criteria', label: '怎样算做完' },
] as const
</script>

<style scoped lang="scss">
.requirements-workbench {
  padding: 28px;
  max-width: 1440px;
  margin: 0 auto;
  line-height: 1.7;
  header {
    margin-bottom: 24px;
  }
  h1 {
    font-size: 26px;
    margin: 12px 0;
  }
  h2 {
    font-size: 20px;
  }
  h3 {
    font-size: 16px;
    margin: 20px 0 8px;
  }
  p {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  li {
    overflow-wrap: anywhere;
  }
  button {
    padding: 9px 16px;
    border-radius: 8px;
    cursor: pointer;
    border: 1px solid var(--color-forge-blue);
    background: var(--color-forge-blue);
    color: white;
  }
  button:disabled {
    opacity: 0.55;
    cursor: default;
  }
  .columns {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
  }
  section {
    min-width: 0;
    border: 1px solid var(--color-forge-line);
    padding: 24px;
    border-radius: 12px;
  }
  .message,
  .questions {
    border: 1px solid var(--color-forge-line);
    padding: 12px 16px;
    margin: 16px 0;
    border-radius: 8px;
  }
  .questions,
  .notice {
    background: var(--color-forge-panel);
  }
  .hint,
  small {
    color: var(--color-forge-muted);
  }
  .error {
    color: #c74343;
    padding: 12px;
    border: 1px solid currentColor;
    border-radius: 8px;
  }
  label {
    display: block;
    margin-top: 16px;
  }
  textarea {
    box-sizing: border-box;
    width: 100%;
    resize: vertical;
    font: inherit;
    color: inherit;
    background: transparent;
    border: 1px solid var(--color-forge-line);
    border-radius: 8px;
    padding: 12px;
    margin: 8px 0;
  }
  @media (max-width: 850px) {
    padding: 16px;
    .columns {
      grid-template-columns: 1fr;
    }
  }
}
</style>
