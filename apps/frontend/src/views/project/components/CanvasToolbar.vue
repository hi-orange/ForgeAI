<template>
  <header class="canvas-toolbar">
    <template v-if="generated && workspaceView === 'viewer'">
      <div class="toolbar-left">
        <button
          type="button"
          class="design-badge"
          :class="{ active: designMode }"
          :title="designMode ? '退出设计模式' : '进入设计模式'"
          @click="$emit('toggle-design')"
        >
          <WorkbenchIcon :name="designMode ? 'design-select' : 'design-idle'" size="sm" />
          Design
        </button>
      </div>

      <div class="toolbar-center preview-nav">
        <button
          type="button"
          class="nav-icon"
          :title="previewMode === 'mobile' ? '显示桌面预览' : '显示手机预览'"
          @click="togglePreviewDevice"
        >
          <WorkbenchIcon :name="previewMode === 'mobile' ? 'desktop' : 'mobile'" size="sm" />
        </button>
        <button type="button" class="nav-icon" title="刷新预览" @click="$emit('refresh')">
          <WorkbenchIcon name="refresh" size="sm" />
        </button>

        <div class="route-bar" ref="pageMenuRef">
          <button
            type="button"
            class="route-home"
            title="首页"
            @click="selectPage(pages[0]?.id || 'home')"
          >
            <WorkbenchIcon name="home" size="sm" />
          </button>
          <button
            type="button"
            class="route-select"
            title="切换页面"
            @click="pageMenuOpen = !pageMenuOpen"
          >
            <span class="route-label">{{ activePageLabel }}</span>
            <WorkbenchIcon name="chevron-down" size="sm" />
          </button>
          <div v-if="pageMenuOpen && pages.length" class="route-menu">
            <button
              v-for="page in pages"
              :key="page.id"
              type="button"
              :class="{ active: page.id === activePageId }"
              @click="selectPage(page.id)"
            >
              <span class="route-menu-label">{{ page.name }}</span>
              <WorkbenchIcon v-if="page.id === activePageId" name="check" size="sm" />
            </button>
          </div>
        </div>
      </div>

      <div class="toolbar-right">
        <button
          type="button"
          class="console-btn"
          :class="{ active: consoleOpen }"
          title="开发者控制台"
          @click="$emit('toggle-console')"
        >
          <WorkbenchIcon name="console" size="sm" />
          Console
        </button>
      </div>
    </template>

    <template v-else-if="generated">
      <div class="toolbar-left">
        <span class="viewer-label">{{ workspaceLabel }}</span>
      </div>
      <div />
      <div />
    </template>

    <template v-else>
      <div class="toolbar-left">
        <span class="viewer-label">应用查看器</span>
        <span :class="['status-pill', status || 'draft']">{{ statusLabel }}</span>
      </div>
      <div class="toolbar-center device-strip">
        <button
          v-for="mode in modes"
          :key="mode.key"
          type="button"
          class="nav-icon"
          :class="{ active: previewMode === mode.key }"
          :title="mode.label"
          @click="$emit('update:previewMode', mode.key)"
        >
          <WorkbenchIcon :name="deviceIconName(mode.key)" size="sm" />
        </button>
      </div>
      <div />
    </template>
  </header>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import type { PreviewMode, WorkspaceView } from '../projectView'
import WorkbenchIcon, { type WorkbenchIconName } from './WorkbenchIcon.vue'

export type PreviewPageOption = {
  id: string
  name: string
  path: string
}

const props = defineProps<{
  generated: boolean
  designMode: boolean
  consoleOpen: boolean
  previewMode: PreviewMode
  workspaceView: WorkspaceView
  status?: string | null
  statusLabel: string
  modes: { key: PreviewMode; label: string }[]
  pages: PreviewPageOption[]
  activePageId: string
}>()

const emit = defineEmits<{
  'update:previewMode': [value: PreviewMode]
  refresh: []
  'update:activePageId': [value: string]
  'toggle-design': []
  'toggle-console': []
}>()

const pageMenuOpen = ref(false)
const pageMenuRef = ref<HTMLElement | null>(null)

const activePageLabel = computed(() => {
  const page = props.pages.find((item) => item.id === props.activePageId)
  return page?.name || 'Home'
})

const workspaceLabel = computed(() => {
  const map: Record<WorkspaceView, string> = {
    viewer: 'App Viewer',
    overview: 'Overview',
    editor: 'Editor',
    files: 'Files',
  }
  return map[props.workspaceView]
})

function deviceIconName(mode: PreviewMode): WorkbenchIconName {
  if (mode === 'mobile') return 'mobile'
  if (mode === 'tablet') return 'tablet'
  return 'desktop'
}

function togglePreviewDevice() {
  emit('update:previewMode', props.previewMode === 'mobile' ? 'desktop' : 'mobile')
}

function selectPage(id: string) {
  pageMenuOpen.value = false
  emit('update:activePageId', id)
}

function onDocumentClick(event: MouseEvent) {
  if (!pageMenuRef.value?.contains(event.target as Node)) {
    pageMenuOpen.value = false
  }
}

onMounted(() => document.addEventListener('click', onDocumentClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocumentClick))
</script>

<style scoped lang="scss">
.canvas-toolbar {
  position: relative;
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  min-height: 2rem;
  gap: 0.75rem;
  padding: 0.2rem 0.5rem;
  border-bottom: 1px solid #f0f0f1;
  background: #fff;
}

.toolbar-left,
.toolbar-center,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.preview-nav {
  position: absolute;
  left: 50%;
  gap: 0.25rem;
  transform: translateX(-50%);
}

.device-strip {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
}

.design-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  height: 2rem;
  border: 0;
  border-radius: 0.5rem;
  padding: 0 0.6rem;
  background: transparent;
  color: #71717a;
  font-size: 0.75rem;
  font-weight: 600;
  cursor: pointer;
  transition:
    background 0.12s ease,
    color 0.12s ease;
}

.design-badge:hover:not(.active) {
  color: #3f3f46;
  background: #f4f4f5;
}

.design-badge.active {
  background: #e8eeff;
  color: #3b5bdb;
}

.design-badge.active:hover {
  background: #dbe4ff;
  color: #2748c7;
}

.nav-icon {
  display: grid;
  place-items: center;
  width: 2rem;
  height: 2rem;
  border: 0;
  border-radius: 0.45rem;
  background: transparent;
  color: #52525b;
  cursor: pointer;
}

.nav-icon:hover,
.nav-icon.active {
  background: #f4f4f5;
  color: #3f3f46;
}

.route-bar {
  position: relative;
  display: flex;
  align-items: center;
  height: 2rem;
  border: 1px solid #e4e4e7;
  border-radius: 999px;
  background: #fafafa;
  overflow: hidden;
}

.route-home {
  display: grid;
  place-items: center;
  width: 2rem;
  height: 100%;
  border: 0;
  border-right: 1px solid #e4e4e7;
  background: transparent;
  color: #52525b;
  cursor: pointer;
}

.route-select {
  display: flex;
  align-items: center;
  gap: 0.25rem;
  min-width: 4.5rem;
  height: 100%;
  border: 0;
  padding: 0 0.55rem 0 0.45rem;
  background: transparent;
  color: #52525b;
  font-size: 0.7rem;
  font-weight: 600;
  cursor: pointer;
}

.route-label {
  white-space: nowrap;
}

.route-menu {
  position: absolute;
  top: calc(100% + 0.35rem);
  left: 0;
  z-index: 20;
  min-width: 10rem;
  padding: 0.3rem;
  border: 1px solid #e4e4e7;
  border-radius: 0.5rem;
  background: #fff;
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.12);
}

.route-menu button {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  width: 100%;
  border: 0;
  border-radius: 0.375rem;
  padding: 0.4rem 0.5rem;
  background: transparent;
  text-align: left;
  font-size: 0.72rem;
  cursor: pointer;
}

.route-menu-label {
  color: #3f3f46;
}

.route-menu button.active,
.route-menu button:hover {
  background: #f4f4f5;
}

.console-btn {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  height: 2rem;
  border: 0;
  border-radius: 0.5rem;
  padding: 0 0.5rem;
  background: transparent;
  color: #18181b;
  font-size: 0.7rem;
  font-weight: 700;
  cursor: pointer;
}

.console-btn:hover,
.console-btn.active {
  background: #f4f4f5;
}

.viewer-label {
  color: #18181b;
  font-size: 0.7rem;
  font-weight: 700;
}

.status-pill {
  border-radius: 999px;
  padding: 0.12rem 0.5rem;
  background: #f1f5f9;
  color: #475569;
  font-size: 0.7rem;
  font-weight: 700;
}
</style>
