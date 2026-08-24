<template>
  <header class="project-topbar">
    <div class="project-menu">
      <ForgeLogo :size="24" />
      <strong>{{ name || 'Untitled Website' }}</strong>
    </div>
    <div class="app-tools">
      <button type="button" class="icon-btn" title="历史记录">↶</button>
      <button type="button" class="icon-btn" title="收起侧栏">≪</button>
      <span class="viewer-pill">▣&nbsp; App Viewer</span>
      <button type="button" class="icon-btn" title="应用">⌘</button>
      <button type="button" class="icon-btn" title="终端">⌁</button>
      <button type="button" class="icon-btn" title="文件">□</button>
      <button type="button" class="icon-btn" title="更多">•••</button>
    </div>
    <div class="publish-tools">
      <button type="button" class="action-btn">分享</button>
      <button type="button" class="action-btn primary" :disabled="status !== 'completed'">
        发布
      </button>
    </div>
  </header>
</template>

<script setup lang="ts">
import ForgeLogo from '@/components/ForgeLogo.vue'

defineProps<{ name?: string | null; status?: string | null }>()
</script>

<style scoped lang="scss">
.project-topbar {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: clamp(360px, 28vw, 500px) minmax(0, 1fr) auto;
  align-items: center;
  min-width: 0;
  padding: 0 0.45rem;
  color: #18181b;
}

.project-menu,
.app-tools,
.publish-tools {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 0.45rem;
}

.project-menu strong {
  max-width: min(22vw, 320px);
  overflow: hidden;
  font-size: 0.8rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.publish-tools {
  justify-content: flex-end;
}

.icon-btn {
  display: grid;
  place-items: center;
  width: 1.8rem;
  height: 1.8rem;
  border: 1px solid #e4e4e7;
  border-radius: 999px;
  background: #fff;
  color: #52525b;
  font-size: 0.72rem;
  cursor: pointer;
}

.viewer-pill {
  display: inline-flex;
  align-items: center;
  height: 1.9rem;
  border-radius: 999px;
  padding: 0 0.85rem;
  background: #e9e9eb;
  font-size: 0.76rem;
  font-weight: 750;
  white-space: nowrap;
}

.action-btn {
  border: 1px solid #e2e8f0;
  border-radius: 0.65rem;
  padding: 0.4rem 0.75rem;
  background: #fff;
  color: #334155;
  font-size: 0.82rem;
  font-weight: 700;
  cursor: pointer;
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

  .app-tools {
    display: none;
  }
}
</style>
