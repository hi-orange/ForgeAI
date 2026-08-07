<template>
  <div class="page">
    <AppSidebar />

    <main class="main">
      <div class="panel">
        <header class="header">
          <RouterLink class="back" :to="{ name: 'home' }">← 返回首页</RouterLink>
          <h1>{{ project?.name || '项目详情' }}</h1>
          <p v-if="project" class="status">
            状态：
            <span :class="['badge', project.status]">{{ statusLabel }}</span>
          </p>
        </header>

        <p v-if="loading" class="hint">加载中…</p>
        <p v-else-if="error" class="error">{{ error }}</p>

        <template v-else-if="project">
          <section class="card">
            <h2>用户需求</h2>
            <p class="prompt">{{ project.prompt }}</p>
          </section>

          <section class="card">
            <h2>Agent Workflow</h2>
            <p class="hint">
              后端已收到需求，项目已进入 Agent Workflow（当前为可运行占位，后续接入真实多 Agent
              流水线）。
            </p>
            <p v-if="workflowId" class="meta">workflow_id: {{ workflowId }}</p>
          </section>
        </template>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import AppSidebar from '@/components/AppSidebar.vue'
import { useProjectStore } from '@/stores/project'

const route = useRoute()
const projects = useProjectStore()

const loading = ref(true)
const error = ref<string | null>(null)
const workflowId = ref<string | null>(
  typeof route.query.workflow_id === 'string' ? route.query.workflow_id : null,
)

const project = computed(() => projects.current)

const statusLabel = computed(() => {
  const map: Record<string, string> = {
    draft: '草稿',
    running: '构建中',
    completed: '已完成',
    failed: '失败',
  }
  return map[project.value?.status || ''] || project.value?.status || '—'
})

onMounted(async () => {
  const id = Number(route.params.id)
  if (!Number.isFinite(id)) {
    error.value = '无效的项目 ID'
    loading.value = false
    return
  }
  try {
    await projects.fetchOne(id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.page {
  display: flex;
  min-height: 100vh;
  background: #f7f8fb;
}

.main {
  flex: 1;
  min-width: 0;
  padding: 2rem 1.5rem 3rem;
}

.panel {
  width: min(100%, 720px);
  margin: 0 auto;
}

.header {
  margin-bottom: 1.25rem;
}

.back {
  display: inline-block;
  margin-bottom: 0.75rem;
  color: #64748b;
  font-size: 0.9rem;
  text-decoration: none;
}

.back:hover {
  color: #2563eb;
}

.header h1 {
  margin: 0 0 0.45rem;
  font-size: 1.45rem;
  font-weight: 750;
  color: #0f172a;
}

.status {
  margin: 0;
  color: #64748b;
  font-size: 0.92rem;
}

.badge {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 0.15rem 0.55rem;
  font-size: 0.82rem;
  font-weight: 700;
}

.badge.draft {
  background: #f1f5f9;
  color: #475569;
}

.badge.running {
  background: #dbeafe;
  color: #1d4ed8;
}

.badge.completed {
  background: #dcfce7;
  color: #15803d;
}

.badge.failed {
  background: #fee2e2;
  color: #b91c1c;
}

.card {
  margin-bottom: 1rem;
  padding: 1.1rem 1.2rem;
  border: 1px solid #e6ebf2;
  border-radius: 1rem;
  background: #fff;
  box-shadow: 0 10px 28px rgba(15, 23, 42, 0.04);
}

.card h2 {
  margin: 0 0 0.65rem;
  font-size: 0.95rem;
  font-weight: 700;
  color: #0f172a;
}

.prompt {
  margin: 0;
  color: #334155;
  line-height: 1.6;
  white-space: pre-wrap;
}

.hint {
  margin: 0;
  color: #64748b;
  line-height: 1.55;
  font-size: 0.92rem;
}

.meta {
  margin: 0.75rem 0 0;
  color: #94a3b8;
  font-size: 0.82rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.error {
  color: #dc2626;
  font-weight: 600;
}
</style>
