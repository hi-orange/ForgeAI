<template>
  <section class="app-viewer" aria-label="App 预览">
    <header class="viewer-toolbar">
      <strong>App 预览</strong>
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
      <div class="viewer-address"><WorkbenchIcon name="home" /> Home</div>
      <div class="viewer-actions">
        <button type="button" :disabled="refreshing" title="重新加载预览状态" @click="reload">
          <WorkbenchIcon name="refresh" />
          <span>刷新</span>
        </button>
        <button
          type="button"
          disabled
          title="安全预览运行时尚未准备好"
          aria-label="在新标签页打开（尚不可用）"
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
            <span>在线预览</span>
          </div>
          <p v-if="status?.code_ready" class="preview-note">
            源码已经生成，可在顶部“编辑器”中查看。通过平台检查并启动隔离运行时后，
            这里才会展示真实应用。
          </p>
        </div>
      </div>
    </div>

    <aside v-if="consoleOpen" class="viewer-console" aria-label="运行日志">
      <div class="console-heading">
        <strong>运行日志</strong>
        <span>{{ consoleRows.length }} 条</span>
      </div>
      <p v-if="!consoleRows.length" class="console-empty">
        开始构建后，文件操作和错误会显示在这里。
      </p>
      <ol v-else>
        <li v-for="row in consoleRows" :key="row.id" :class="{ failed: !row.ok }">
          <span>{{ row.ok ? '●' : '×' }}</span>
          <strong>{{ row.label }}</strong>
          <code v-if="row.detail">{{ row.detail }}</code>
        </li>
      </ol>
    </aside>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { RequirementsStatus } from '@/api/modules/requirements'
import type { RequirementPlanItem } from '../useRequirements'
import WorkbenchIcon, { type WorkbenchIconName } from './WorkbenchIcon.vue'

type PreviewDevice = 'desktop' | 'tablet' | 'mobile'

const props = defineProps<{
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
const devices: Array<{ id: PreviewDevice; label: string; icon: WorkbenchIconName }> = [
  { id: 'desktop', label: '桌面', icon: 'desktop' },
  { id: 'tablet', label: '平板', icon: 'tablet' },
  { id: 'mobile', label: '手机', icon: 'mobile' },
]

const frameStyle = computed(() => ({
  width: ({ desktop: '100%', tablet: '768px', mobile: '390px' } as const)[device.value],
}))
const selectedItems = computed(() => props.planItems.filter((item) => item.checked))
const showPlanOverview = computed(() => Boolean(props.status?.app_spec) && props.canApprove)
const postApproval = computed(() =>
  ['design_pending', 'design_running', 'engineering_running', 'engineering_generated'].includes(
    props.status?.state ?? '',
  ),
)
const planReady = computed(
  () => props.canApprove || postApproval.value || props.status?.state === 'ready_for_design',
)
const previewTitle = computed(() => {
  if (props.status?.code_ready) return '应用代码已生成'
  if (postApproval.value) return '正在构建你的应用'
  if (props.canApprove) return '你的应用，即将从这里开始'
  return '把想法变成看得见的应用'
})
const previewDescription = computed(() => {
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
  return rows
})

function reload() {
  emit('refresh')
}
</script>

<style scoped lang="scss">
.app-viewer {
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
  background: #f5f5f7;
}
.viewer-toolbar {
  display: grid;
  grid-template-columns: auto auto minmax(120px, 1fr) auto;
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
}
.viewer-stage {
  display: flex;
  flex: 1;
  min-height: 0;
  justify-content: center;
  overflow: auto;
  padding: 16px;
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
.viewer-error button {
  margin-top: 18px;
  border: 0;
  border-radius: 8px;
  background: #5b5bd6;
  color: #fff;
  padding: 9px 15px;
  cursor: pointer;
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
    grid-template-columns: auto auto 1fr;
  }
  .viewer-address {
    display: none;
  }
  .viewer-actions span {
    display: none;
  }
}
</style>
