<template>
  <aside class="sidebar" :class="{ collapsed }">
    <div class="sidebar-top">
      <div class="brand-row">
        <div v-show="!collapsed" class="brand">
          <ForgeLogo :size="26" />
          <span>Forge</span>
        </div>
        <button
          type="button"
          class="icon-btn panel-btn"
          :title="collapsed ? '展开侧边栏' : '折叠侧边栏'"
          :aria-label="collapsed ? '展开侧边栏' : '折叠侧边栏'"
          :aria-expanded="!collapsed"
          @click="toggleCollapse"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <rect x="3.5" y="3.5" width="17" height="17" rx="2.5" />
            <path d="M9.5 3.5v17" />
          </svg>
        </button>
      </div>

      <button
        type="button"
        class="workspace"
        :title="workspaceLabel"
        @click="openSettings('workspace')"
      >
        <span class="avatar">{{ initials }}</span>
        <span v-show="!collapsed" class="workspace-text">
          <span class="workspace-name">{{ workspaceLabel }}</span>
        </span>
      </button>

      <nav class="nav">
        <RouterLink
          v-for="item in navItems"
          :key="item.key"
          :to="item.to"
          class="nav-item"
          :class="{ active: isActive(item.to) }"
          :title="item.label"
        >
          <span class="nav-icon" aria-hidden="true">
            <svg
              v-if="item.icon === 'home'"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
            >
              <path d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1v-9.5Z" />
            </svg>
            <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <path
                d="M4 8.5A2.5 2.5 0 0 1 6.5 6H11l2 2h4.5A2.5 2.5 0 0 1 20 10.5V17a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8.5Z"
              />
            </svg>
          </span>
          <span v-show="!collapsed">{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div v-show="!collapsed" class="section">
        <p class="section-title">最近</p>
        <button v-for="item in recentItems" :key="item.id" type="button" class="recent-item">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <circle cx="12" cy="12" r="8" />
            <path d="M12 8v4l2.5 2.5" />
          </svg>
          <span>{{ item.title }}</span>
        </button>
      </div>
    </div>

    <div class="sidebar-bottom">
      <div class="user-bar">
        <button
          type="button"
          class="user-avatar"
          :title="displayName"
          @click="openSettings('account')"
        >
          {{ initials }}
        </button>
        <div v-show="!collapsed" class="user-actions">
          <button type="button" class="icon-btn" title="设置" @click="openSettings('account')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <circle cx="12" cy="12" r="3" />
              <path
                d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9c.3.6.9 1 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"
              />
            </svg>
          </button>
          <button type="button" class="icon-btn" title="通知">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <path d="M6 9a6 6 0 1 1 12 0c0 7 3 7 3 7H3s3 0 3-7Z" />
              <path d="M10 19a2 2 0 0 0 4 0" />
            </svg>
          </button>
        </div>
      </div>
    </div>

    <SettingsModal v-model:open="settingsOpen" :default-tab="settingsTab" />
  </aside>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'

import ForgeLogo from '@/components/ForgeLogo.vue'
import SettingsModal from '@/components/SettingsModal.vue'
import { useAuthStore } from '@/stores/auth'

const emit = defineEmits<{
  collapse: [collapsed: boolean]
}>()

const auth = useAuthStore()
const route = useRoute()
const collapsed = ref(false)
const settingsOpen = ref(false)
const settingsTab = ref<'workspace' | 'account'>('account')

function openSettings(tab: 'workspace' | 'account' = 'account') {
  settingsTab.value = tab
  settingsOpen.value = true
}

function toggleCollapse() {
  collapsed.value = !collapsed.value
  emit('collapse', collapsed.value)
}

const displayName = computed(() => {
  const raw = auth.user?.username || auth.user?.email?.split('@')[0] || 'user'
  return raw.replace(/[._-]+/g, ' ').trim()
})

const initials = computed(() => {
  const parts = displayName.value.split(/\s+/).filter(Boolean)
  if (parts.length >= 2) {
    return (parts[0]![0]! + parts[1]![0]!).toUpperCase()
  }
  return displayName.value.slice(0, 2).toUpperCase()
})

const workspaceLabel = computed(() => `${displayName.value}'s Forge`)

const navItems = [
  { key: 'home', label: '首页', to: '/', icon: 'home' },
  { key: 'projects', label: '我的项目', to: '#projects', icon: 'folder' },
] as const

const recentItems = [{ id: '1', title: '招聘网站设计' }]

function isActive(to: string) {
  if (to === '/') return route.path === '/'
  return false
}
</script>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  width: 260px;
  min-width: 260px;
  height: 100vh;
  padding: 1rem 0.85rem 0.85rem;
  background: #f3f5f9;
  border-right: 1px solid #e8ecf2;
  box-sizing: border-box;
  transition:
    width 0.2s ease,
    min-width 0.2s ease,
    padding 0.2s ease;
}

.sidebar.collapsed {
  width: 72px;
  min-width: 72px;
  padding: 1rem 0.55rem 0.85rem;
}

.sidebar-top {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
  min-height: 0;
}

.brand-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 0.25rem;
  gap: 0.35rem;
}

.sidebar.collapsed .brand-row {
  justify-content: center;
  padding: 0;
}

.brand {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  min-width: 0;
  font-family: var(--font-sans);
  font-size: 1.2rem;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: #0f172a;
}

.icon-btn {
  display: grid;
  place-items: center;
  width: 2rem;
  height: 2rem;
  border: 0;
  border-radius: 0.55rem;
  background: transparent;
  color: #64748b;
  cursor: pointer;
  flex-shrink: 0;
}

.icon-btn:hover {
  background: #e8eef8;
  color: #1d4ed8;
}

.icon-btn svg {
  width: 1.05rem;
  height: 1.05rem;
}

.panel-btn svg {
  width: 1.15rem;
  height: 1.15rem;
}

.workspace {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  width: 100%;
  padding: 0.55rem 0.6rem;
  border: 1px solid #e2e8f0;
  border-radius: 0.85rem;
  background: #fff;
  cursor: pointer;
  text-align: left;
}

.sidebar.collapsed .workspace {
  justify-content: center;
  padding: 0.45rem;
}

.workspace:hover {
  border-color: #c7d2fe;
}

.avatar,
.user-avatar {
  display: grid;
  place-items: center;
  width: 1.85rem;
  height: 1.85rem;
  border-radius: 0.55rem;
  background: linear-gradient(145deg, #60a5fa, #2563eb);
  color: #fff;
  font-size: 0.68rem;
  font-weight: 700;
  flex-shrink: 0;
}

.workspace-text {
  min-width: 0;
  flex: 1;
}

.workspace-name {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #0f172a;
  font-size: 0.88rem;
  font-weight: 600;
}

.nav {
  display: grid;
  gap: 0.2rem;
  margin-top: 0.25rem;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  padding: 0.65rem 0.75rem;
  border-radius: 0.75rem;
  color: #334155;
  font-size: 0.92rem;
  font-weight: 550;
}

.sidebar.collapsed .nav-item {
  justify-content: center;
  padding: 0.65rem 0.4rem;
}

.nav-item:hover {
  background: #e9eef7;
}

.nav-item.active {
  background: #e0ebff;
  color: #1d4ed8;
  font-weight: 700;
}

.nav-icon {
  display: grid;
  place-items: center;
  width: 1.15rem;
  height: 1.15rem;
}

.nav-icon svg {
  width: 100%;
  height: 100%;
}

.section {
  margin-top: 0.85rem;
}

.section-title {
  margin: 0 0 0.4rem;
  padding: 0 0.75rem;
  color: #94a3b8;
  font-size: 0.78rem;
  font-weight: 600;
}

.recent-item {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  width: 100%;
  padding: 0.55rem 0.75rem;
  border: 0;
  border-radius: 0.7rem;
  background: transparent;
  color: #475569;
  font-size: 0.88rem;
  cursor: pointer;
  text-align: left;
}

.recent-item:hover {
  background: #e9eef7;
}

.recent-item svg {
  width: 1rem;
  height: 1rem;
  color: #94a3b8;
  flex-shrink: 0;
}

.sidebar-bottom {
  display: grid;
  gap: 0.55rem;
}

.user-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.35rem 0.2rem 0.15rem;
}

.sidebar.collapsed .user-bar {
  justify-content: center;
  padding: 0.35rem 0 0.15rem;
}

.user-avatar {
  border: 0;
  cursor: pointer;
}

.user-actions {
  display: flex;
  align-items: center;
  gap: 0.15rem;
}

@media (max-width: 900px) {
  .sidebar {
    display: none;
  }
}
</style>
