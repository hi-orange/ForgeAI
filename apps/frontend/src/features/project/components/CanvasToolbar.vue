<template>
  <header class="canvas-toolbar">
    <template v-if="generated">
      <div class="toolbar-left">
        <button
          type="button"
          class="design-badge"
          :class="{ active: designMode }"
          @click="$emit('toggle-design')"
        >
          ⌖ Design
        </button>
      </div>
      <div class="toolbar-center preview-nav">
        <button
          v-for="mode in modes"
          :key="mode.key"
          type="button"
          class="preview-icon"
          :class="{ active: previewMode === mode.key }"
          :title="mode.label"
          @click="$emit('update:previewMode', mode.key)"
        >
          {{ mode.icon }}
        </button>
        <button type="button" class="nav-icon" title="刷新">↻</button>
        <button type="button" class="page-select">Home <span>⌄</span></button>
        <button type="button" class="nav-icon" title="新窗口">↗</button>
      </div>
      <span class="console">&lt;&gt; Console</span>
    </template>
    <template v-else>
      <div class="toolbar-left">
        <span class="viewer-label">应用查看器</span>
        <span :class="['status-pill', status || 'draft']">{{ statusLabel }}</span>
      </div>
      <div class="toolbar-center">
        <button
          v-for="mode in modes"
          :key="mode.key"
          type="button"
          class="preview-icon"
          :class="{ active: previewMode === mode.key }"
          :title="mode.label"
          @click="$emit('update:previewMode', mode.key)"
        >
          {{ mode.icon }}
        </button>
      </div>
      <div />
    </template>
  </header>
</template>

<script setup lang="ts">
import type { PreviewMode } from '../types/projectView'

defineProps<{
  generated: boolean
  designMode: boolean
  previewMode: PreviewMode
  status?: string | null
  statusLabel: string
  modes: { key: PreviewMode; label: string; icon: string }[]
}>()

defineEmits<{
  'toggle-design': []
  'update:previewMode': [value: PreviewMode]
}>()
</script>

<style scoped lang="scss">
.canvas-toolbar {
  position: relative;
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  min-height: 2.25rem;
  gap: 1rem;
  padding: 0.25rem 0.55rem;
  border-bottom: 1px solid #eeeeef;
  background: #fff;
}

.toolbar-left,
.toolbar-center {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.design-badge {
  display: inline-flex;
  align-items: center;
  height: 1.6rem;
  border: 0;
  border-radius: 0.5rem;
  padding: 0 0.55rem;
  background: #eef2ff;
  color: #4f46e5;
  font-size: 0.72rem;
  font-weight: 700;
  cursor: pointer;
}

.design-badge.active {
  background: #e0e7ff;
  color: #4338ca;
}

.preview-nav {
  position: absolute;
  left: 50%;
  gap: 0.25rem;
  transform: translateX(-50%);
}

.preview-icon,
.nav-icon {
  display: grid;
  place-items: center;
  width: 1.65rem;
  height: 1.65rem;
  border: 1px solid transparent;
  border-radius: 0.55rem;
  background: transparent;
  color: #52525b;
  font-size: 0.7rem;
  cursor: pointer;
}

.preview-icon.active,
.preview-icon:hover {
  border-color: #bfdbfe;
  background: #eff6ff;
}

.page-select {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 3.5rem;
  min-width: 11.5rem;
  height: 1.55rem;
  border: 1px solid #e4e4e7;
  border-radius: 999px;
  background: #fafafa;
  color: #52525b;
  font-size: 0.7rem;
}

.console,
.viewer-label {
  color: #18181b;
  font-size: 0.72rem;
  font-weight: 700;
}

.status-pill {
  border-radius: 999px;
  padding: 0.15rem 0.55rem;
  background: #f1f5f9;
  color: #475569;
  font-size: 0.75rem;
  font-weight: 700;
}
</style>
