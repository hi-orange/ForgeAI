<template>
  <div class="workspace-panels">
    <aside v-if="historyOpen" class="history-panel" aria-label="版本历史">
      <header class="panel-head">
        <h2>历史记录</h2>
        <button type="button" class="icon-close" title="关闭" @click="$emit('close-history')">
          ×
        </button>
      </header>
      <div class="history-tabs">
        <span class="history-tab active">版本</span>
      </div>
      <ul class="history-list">
        <li v-for="entry in historyEntries" :key="entry.id" class="history-card">
          <div class="history-thumb" aria-hidden="true">
            <span>{{ entry.label.slice(0, 1) }}</span>
          </div>
          <div class="history-meta">
            <strong>{{ entry.label }}</strong>
            <p>{{ entry.time }}</p>
            <span class="history-tag">v{{ entry.revision }}</span>
          </div>
        </li>
      </ul>
    </aside>

    <section v-if="workspaceView === 'overview'" class="overview-panel">
      <header class="overview-head">
        <div class="overview-app-icon" aria-hidden="true">T</div>
        <div>
          <h2>{{ productName || '未命名项目' }}</h2>
          <p>{{ productSummary || '发布后可分享链接并管理线上版本。' }}</p>
        </div>
      </header>
      <div class="overview-grid">
        <article class="overview-launch">
          <h3>发布应用</h3>
          <p>发布后项目将上线并解锁分享工具，发布后仍可继续编辑。</p>
          <button
            type="button"
            class="launch-btn"
            :disabled="status !== 'completed'"
            @click="$emit('publish')"
          >
            发布应用
          </button>
        </article>
        <article class="overview-card">
          <span class="card-icon" aria-hidden="true">🚀</span>
          <h4>线上版本</h4>
          <p>选择当前对外展示的版本。</p>
          <span class="card-meta">v{{ websiteRevision }}</span>
        </article>
        <article class="overview-card">
          <span class="card-icon" aria-hidden="true">🌐</span>
          <h4>域名绑定</h4>
          <p>配置主域名与自定义域名。</p>
        </article>
        <article class="overview-card">
          <span class="card-icon" aria-hidden="true">🔍</span>
          <h4>SEO 与社交</h4>
          <p>管理搜索引擎与社交分享元数据。</p>
        </article>
        <article class="overview-card">
          <span class="card-icon" aria-hidden="true">📊</span>
          <h4>数据分析</h4>
          <p>接入访问统计与转化追踪。</p>
        </article>
        <article class="overview-card">
          <span class="card-icon" aria-hidden="true">🗄</span>
          <h4>生产资源</h4>
          <p>连接数据库、存储与 API 密钥。</p>
        </article>
      </div>
    </section>

    <EditorWorkspace
      v-else-if="workspaceView === 'editor'"
      class="editor-workspace"
      :files="files"
      :product-name="productName"
      @download-file="$emit('download-file', $event)"
      @download-all="$emit('download-all')"
    />

    <section v-else-if="workspaceView === 'files'" class="files-panel">
      <header class="files-head">
        <p class="files-breadcrumb">data / chats / {{ productName || '项目' }}</p>
        <div class="files-actions">
          <button type="button" class="files-btn" @click="$emit('download-all')">下载全部</button>
        </div>
      </header>
      <table class="files-table">
        <thead>
          <tr>
            <th>文件名</th>
            <th>大小</th>
            <th>最后更新</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in fileRows" :key="row.name">
            <td>
              <span class="file-type" aria-hidden="true">{{ row.icon }}</span>
              {{ row.name }}
            </td>
            <td>{{ row.size }}</td>
            <td>{{ row.updated }}</td>
            <td>
              <button
                type="button"
                class="file-action"
                title="下载"
                @click="$emit('download-file', row.name)"
              >
                ↓
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { GeneratedWebsiteFiles } from '@/api/modules/project'
import type { EditorFileKey, WorkspaceView } from '../projectView'
import EditorWorkspace from './EditorWorkspace.vue'

const props = defineProps<{
  workspaceView: WorkspaceView
  historyOpen: boolean
  files: GeneratedWebsiteFiles
  websiteRevision: number
  status?: string | null
  productName?: string | null
  productSummary?: string | null
  updatedAt?: string | null
}>()

defineEmits<{
  'close-history': []
  publish: []
  'download-file': [name: EditorFileKey]
  'download-all': []
}>()

const editorFiles: EditorFileKey[] = ['index.html', 'style.css', 'script.js']

const historyEntries = computed(() => [
  {
    id: 'current',
    label: '当前网站版本',
    time: formatTime(props.updatedAt),
    revision: props.websiteRevision,
  },
  ...(props.websiteRevision > 1
    ? [
        {
          id: 'prev',
          label: '上一版构建',
          time: '较早版本',
          revision: props.websiteRevision - 1,
        },
      ]
    : []),
])

const fileRows = computed(() =>
  editorFiles.map((name) => ({
    name,
    icon: name.endsWith('.html') ? '{}' : name.endsWith('.css') ? '◧' : 'JS',
    size: formatSize(props.files[name]?.length ?? 0),
    updated: formatTime(props.updatedAt),
  })),
)

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  return `${(bytes / 1024).toFixed(2)} KB`
}

function formatTime(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
</script>

<style scoped lang="scss">
.workspace-panels {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
}

.history-panel {
  position: absolute;
  inset: 0 auto 0 0;
  z-index: 30;
  width: min(320px, 92%);
  display: flex;
  flex-direction: column;
  border-right: 1px solid #e4e4e7;
  background: #fff;
  box-shadow: 8px 0 24px rgba(15, 23, 42, 0.08);
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.85rem 1rem;
  border-bottom: 1px solid #f1f5f9;
}

.panel-head h2 {
  margin: 0;
  font-size: 0.95rem;
}

.icon-close {
  width: 1.75rem;
  height: 1.75rem;
  border: 0;
  border-radius: 0.5rem;
  background: transparent;
  font-size: 1.1rem;
  cursor: pointer;
}

.history-tabs {
  padding: 0.5rem 1rem;
}

.history-tab {
  font-size: 0.78rem;
  font-weight: 700;
  color: #2563eb;
}

.history-list {
  list-style: none;
  margin: 0;
  padding: 0.5rem 0.75rem 1rem;
  overflow: auto;
}

.history-card {
  display: flex;
  gap: 0.65rem;
  padding: 0.65rem;
  border-radius: 0.75rem;
  background: #f5f3ff;
}

.history-thumb {
  width: 2.5rem;
  height: 2.5rem;
  border-radius: 0.5rem;
  background: #e9d5ff;
  display: grid;
  place-items: center;
  font-weight: 800;
  color: #6d28d9;
}

.history-meta strong {
  display: block;
  font-size: 0.82rem;
}

.history-meta p {
  margin: 0.15rem 0 0;
  font-size: 0.72rem;
  color: #64748b;
}

.history-tag {
  display: inline-block;
  margin-top: 0.25rem;
  padding: 0.1rem 0.35rem;
  border-radius: 0.35rem;
  background: #ede9fe;
  color: #5b21b6;
  font-size: 0.68rem;
  font-weight: 700;
}

.overview-panel,
.files-panel {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 1.25rem 1.5rem;
  background: #fafafa;
}

.overview-head {
  display: flex;
  gap: 1rem;
  align-items: center;
  margin-bottom: 1.5rem;
}

.overview-app-icon {
  width: 3rem;
  height: 3rem;
  border-radius: 0.85rem;
  background: linear-gradient(135deg, #3b82f6, #6366f1);
  color: #fff;
  display: grid;
  place-items: center;
  font-size: 1.25rem;
  font-weight: 800;
}

.overview-head h2 {
  margin: 0;
  font-size: 1.1rem;
}

.overview-head p {
  margin: 0.25rem 0 0;
  color: #64748b;
  font-size: 0.85rem;
}

.overview-grid {
  display: grid;
  grid-template-columns: 1.1fr 1fr 1fr;
  gap: 0.85rem;
}

.overview-launch {
  grid-row: span 2;
  padding: 1.25rem;
  border-radius: 1rem;
  background: #fff;
  border: 1px solid #e4e4e7;
}

.overview-launch h3 {
  margin: 0 0 0.5rem;
  font-size: 1.35rem;
}

.overview-launch p {
  margin: 0 0 1rem;
  color: #64748b;
  font-size: 0.85rem;
  line-height: 1.5;
}

.launch-btn {
  border: 0;
  border-radius: 0.65rem;
  padding: 0.65rem 1rem;
  background: #18181b;
  color: #fff;
  font-weight: 700;
  cursor: pointer;
}

.launch-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.overview-card {
  padding: 1rem;
  border-radius: 1rem;
  background: #fff;
  border: 1px solid #e4e4e7;
}

.overview-card h4 {
  margin: 0.35rem 0 0.25rem;
  font-size: 0.9rem;
}

.overview-card p {
  margin: 0;
  color: #64748b;
  font-size: 0.78rem;
  line-height: 1.45;
}

.card-meta {
  display: inline-block;
  margin-top: 0.5rem;
  font-size: 0.72rem;
  font-weight: 700;
  color: #2563eb;
}

.editor-workspace {
  flex: 1;
  min-height: 0;
  min-width: 0;
  display: flex;
}

.files-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.75rem;
}

.files-breadcrumb {
  margin: 0;
  font-size: 0.78rem;
  color: #64748b;
}

.files-btn {
  border: 1px solid #e4e4e7;
  border-radius: 0.5rem;
  padding: 0.35rem 0.75rem;
  background: #fff;
  font-size: 0.78rem;
  cursor: pointer;
}

.files-table {
  width: 100%;
  border-collapse: collapse;
  background: #fff;
  border: 1px solid #e4e4e7;
  border-radius: 0.75rem;
  overflow: hidden;
}

.files-table th,
.files-table td {
  padding: 0.65rem 0.85rem;
  border-bottom: 1px solid #f1f5f9;
  font-size: 0.8rem;
  text-align: left;
}

.files-table th {
  background: #f8fafc;
  color: #64748b;
  font-weight: 600;
}

.file-type {
  margin-right: 0.35rem;
  color: #94a3b8;
}

.file-action {
  border: 0;
  background: transparent;
  cursor: pointer;
  color: #475569;
}

@media (max-width: 960px) {
  .overview-grid {
    grid-template-columns: 1fr;
  }

  .overview-launch {
    grid-row: auto;
  }
}
</style>
