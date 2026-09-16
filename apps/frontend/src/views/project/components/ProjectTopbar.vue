<template>
  <header class="project-topbar">
    <div class="topbar-start">
      <div class="project-menu">
        <slot name="brand">
          <ForgeLogo :size="22" />
          <strong>{{ name || '未命名项目' }}</strong>
        </slot>
      </div>
      <div class="topbar-start-tools">
        <button
          type="button"
          class="icon-btn"
          title="历史"
          :class="{ 'is-active': historyOpen }"
          @click="$emit('toggle-history')"
        >
          <WorkbenchIcon name="history" />
        </button>
        <button
          type="button"
          class="icon-btn"
          :title="chatCollapsed ? '展开对话' : '收起对话'"
          @click="$emit('toggle-chat')"
        >
          <WorkbenchIcon :name="chatCollapsed ? 'chat-expand' : 'chat-collapse'" />
        </button>
      </div>
    </div>

    <div class="topbar-center">
      <div class="toolbar-strip" role="toolbar" aria-label="工作区模式">
        <button
          v-for="tab in resolvedTabs"
          :key="tab.id"
          type="button"
          role="tab"
          class="mode-tab"
          :class="{ 'is-active': workspaceView === tab.id }"
          :aria-selected="workspaceView === tab.id"
          :title="tab.label"
          @click="selectMode(tab.id)"
        >
          <WorkbenchIcon :name="tab.icon" />
          <span v-if="workspaceView === tab.id" class="mode-tab-label">{{ tab.label }}</span>
        </button>
      </div>
    </div>

    <div class="publish-tools">
      <button type="button" class="action-btn" @click="$emit('share')">分享</button>
      <button type="button" class="action-btn muted" disabled title="即将推出">升级</button>
      <button
        type="button"
        class="action-btn primary"
        :disabled="!canPublish"
        @click="$emit('publish')"
      >
        发布
      </button>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import ForgeLogo from '@/components/ForgeLogo.vue'
import type { WorkspaceView } from '../projectView'
import WorkbenchIcon, { type WorkbenchIconName } from './WorkbenchIcon.vue'

export type TopbarModeTab = {
  id: WorkspaceView
  icon: WorkbenchIconName
  label: string
}

const props = withDefaults(
  defineProps<{
    name?: string | null
    workspaceView: WorkspaceView
    chatCollapsed: boolean
    historyOpen?: boolean
    canPublish?: boolean
    modeTabs?: TopbarModeTab[]
  }>(),
  {
    historyOpen: false,
    canPublish: false,
  },
)

const emit = defineEmits<{
  'update:workspaceView': [value: WorkspaceView]
  share: []
  publish: []
  'toggle-chat': []
  'toggle-history': []
}>()

const resolvedTabs = computed(() => props.modeTabs ?? [])

function selectMode(view: WorkspaceView) {
  emit('update:workspaceView', view)
}
</script>

<style scoped lang="scss">
.project-topbar {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: clamp(360px, 28vw, 500px) minmax(0, 1fr) auto;
  align-items: center;
  column-gap: 0.5rem;
  min-width: 0;
  min-height: 2.35rem;
  padding: 0 0.25rem;
  color: #18181b;
}

.topbar-start {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  min-width: 0;
}

.project-menu {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  min-width: 0;
  flex: 1 1 auto;
}

.project-menu :deep(strong),
.project-menu strong {
  overflow: hidden;
  font-size: 0.8125rem;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.topbar-start-tools {
  display: flex;
  align-items: center;
  gap: 0.15rem;
  flex-shrink: 0;
  margin-left: auto;
}

.topbar-center {
  display: flex;
  align-items: center;
  justify-content: center;
  min-width: 0;
}

.toolbar-strip {
  display: inline-flex;
  align-items: center;
  gap: 0.15rem;
  width: fit-content;
  max-width: 100%;
  padding: 0.2rem 0.3rem;
  border-radius: 999px;
  background: #f4f4f5;
}

.mode-tab,
.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.35rem;
  height: 2rem;
  border: 0;
  border-radius: 999px;
  background: transparent;
  color: #52525b;
  cursor: pointer;
}

.icon-btn,
.mode-tab:not(.is-active) {
  width: 2rem;
  padding: 0;
}

.mode-tab.is-active {
  width: auto;
  padding: 0 0.7rem;
  background: #fff;
  color: #18181b;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
}

.mode-tab:hover,
.icon-btn:hover:not(:disabled) {
  color: #18181b;
}

.icon-btn.is-active {
  background: #fff;
  color: #18181b;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
}

.mode-tab-label {
  font-size: 0.75rem;
  font-weight: 600;
  line-height: 1;
  white-space: nowrap;
}

.publish-tools {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  justify-self: end;
  flex-shrink: 0;
}

.action-btn {
  border: 1px solid #e4e4e7;
  border-radius: 0.5rem;
  padding: 0.35rem 0.7rem;
  background: #fff;
  color: #3f3f46;
  font-size: 0.78rem;
  font-weight: 600;
  cursor: pointer;
}

.action-btn.muted {
  color: #a1a1aa;
}

.action-btn.primary {
  border-color: #2563eb;
  background: #2563eb;
  color: #fff;
}

.action-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

@media (max-width: 960px) {
  .project-topbar {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .topbar-center {
    display: none;
  }
}
</style>
