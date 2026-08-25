<template>
  <section class="editor-workspace">
    <aside class="editor-sidebar">
      <div class="editor-search">
        <input v-model="searchQuery" type="search" placeholder="Search" aria-label="搜索文件" />
      </div>

      <div class="editor-tree" role="tree">
        <button type="button" class="tree-folder depth-0" @click="toggleFolder('root')">
          <span class="tree-chevron" :class="{ expanded: rootExpanded }">
            <WorkbenchIcon name="chevron-right" size="sm" />
          </span>
          <WorkbenchIcon name="folder" size="sm" />
          <span class="tree-label">{{ rootLabel }}</span>
        </button>

        <template v-if="rootExpanded">
          <button type="button" class="tree-folder depth-1" @click="toggleFolder('src')">
            <span class="tree-chevron" :class="{ expanded: srcExpanded }">
              <WorkbenchIcon name="chevron-right" size="sm" />
            </span>
            <WorkbenchIcon name="folder" size="sm" />
            <span class="tree-label">src</span>
          </button>

          <template v-if="srcExpanded">
            <button
              v-for="file in visibleFiles"
              :key="file"
              type="button"
              class="tree-file depth-2"
              :class="{ active: activeFile === file }"
              @click="selectFile(file)"
            >
              <span class="file-badge" :class="fileBadgeClass(file)">{{
                fileBadgeLabel(file)
              }}</span>
              <span class="tree-label">{{ file }}</span>
            </button>
          </template>
        </template>
      </div>

      <button type="button" class="editor-download-project" @click="$emit('download-all')">
        <WorkbenchIcon name="download" size="sm" />
        Download Project
      </button>
    </aside>

    <div class="editor-main">
      <header class="editor-tab-bar">
        <div class="editor-tab active">
          <span class="file-badge" :class="fileBadgeClass(activeFile)">{{
            fileBadgeLabel(activeFile)
          }}</span>
          <span class="editor-tab-name">{{ activeFile }}</span>
          <button
            type="button"
            class="editor-tab-close"
            title="关闭"
            aria-label="关闭标签"
            @click="closeTab"
          >
            ×
          </button>
        </div>
        <button
          type="button"
          class="editor-tab-download"
          title="下载文件"
          @click="$emit('download-file', activeFile)"
        >
          <WorkbenchIcon name="download" size="sm" />
        </button>
      </header>

      <div class="editor-scroll">
        <div class="editor-lines" aria-hidden="true">
          <span v-for="line in lineCount" :key="line">{{ line }}</span>
        </div>
        <pre class="editor-code" tabindex="0"><code v-html="highlightedCode" /></pre>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import type { GeneratedWebsiteFiles } from '@/api/modules/project'
import { highlightEditorCode } from '../editorHighlight'
import type { EditorFileKey } from '../projectView'
import WorkbenchIcon from './WorkbenchIcon.vue'

const editorFiles: EditorFileKey[] = ['index.html', 'style.css', 'script.js']

function fileBadgeClass(file: EditorFileKey) {
  if (file.endsWith('.html')) return 'html'
  if (file.endsWith('.css')) return 'css'
  return 'js'
}

function fileBadgeLabel(file: EditorFileKey) {
  if (file.endsWith('.html')) return '<>'
  if (file.endsWith('.css')) return '#'
  return 'JS'
}

const props = defineProps<{
  files: GeneratedWebsiteFiles
  productName?: string | null
}>()

defineEmits<{
  'download-file': [name: EditorFileKey]
  'download-all': []
}>()

const activeFile = ref<EditorFileKey>('index.html')
const searchQuery = ref('')
const rootExpanded = ref(true)
const srcExpanded = ref(true)

const rootLabel = computed(() => props.productName?.trim() || 'website')

const visibleFiles = computed(() => {
  const query = searchQuery.value.trim().toLowerCase()
  if (!query) return editorFiles
  return editorFiles.filter((file) => file.toLowerCase().includes(query))
})

const activeContent = computed(() => props.files[activeFile.value] ?? '')
const lineCount = computed(() => Math.max(1, activeContent.value.split('\n').length))
const highlightedCode = computed(() => highlightEditorCode(activeContent.value, activeFile.value))

function selectFile(fileKey: EditorFileKey) {
  activeFile.value = fileKey
}

function toggleFolder(id: 'root' | 'src') {
  if (id === 'root') rootExpanded.value = !rootExpanded.value
  if (id === 'src') srcExpanded.value = !srcExpanded.value
}

function closeTab() {
  const next = editorFiles.find((file) => file !== activeFile.value)
  if (next) activeFile.value = next
}
</script>

<style scoped lang="scss">
.editor-workspace {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  overflow: hidden;
  background: #fff;
}

.editor-sidebar {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-right: 1px solid #e4e4e7;
  background: #fff;
}

.editor-search {
  padding: 0.55rem 0.65rem;
  border-bottom: 1px solid #f0f0f1;
}

.editor-search input {
  width: 100%;
  border: 1px solid #e4e4e7;
  border-radius: 0.5rem;
  padding: 0.4rem 0.55rem;
  font-size: 0.75rem;
  color: #3f3f46;
  background: #fafafa;
  outline: none;
}

.editor-search input:focus {
  border-color: #d4d4d8;
  background: #fff;
}

.editor-tree {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 0.25rem 0;
}

.tree-folder,
.tree-file {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  width: 100%;
  border: 0;
  background: transparent;
  text-align: left;
  font-size: 0.75rem;
  color: #3f3f46;
  cursor: pointer;
  padding-top: 0.28rem;
  padding-bottom: 0.28rem;
  padding-right: 0.5rem;
}

.tree-folder.depth-0 {
  padding-left: 0.35rem;
}

.tree-folder.depth-1 {
  padding-left: 1rem;
}

.tree-file.depth-2 {
  padding-left: 1.65rem;
}

.tree-folder:hover,
.tree-file:hover {
  background: #f4f4f5;
}

.tree-file.active {
  background: #eff6ff;
  color: #1d4ed8;
}

.tree-chevron {
  display: inline-flex;
  width: 0.85rem;
  color: #a1a1aa;
  transition: transform 0.15s ease;
}

.tree-chevron.expanded {
  transform: rotate(90deg);
}

.tree-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-badge {
  flex-shrink: 0;
  width: 1.1rem;
  height: 1.1rem;
  border-radius: 0.2rem;
  display: grid;
  place-items: center;
  font-size: 0.55rem;
  font-weight: 800;
  line-height: 1;
  color: #fff;
}

.file-badge.html {
  background: #3b82f6;
}

.file-badge.css {
  background: #6366f1;
  font-size: 0.62rem;
}

.file-badge.js {
  background: #f59e0b;
}

.editor-download-project {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.4rem;
  margin: 0.5rem 0.65rem 0.65rem;
  border: 1px solid #e4e4e7;
  border-radius: 0.5rem;
  padding: 0.45rem 0.55rem;
  background: #fff;
  font-size: 0.72rem;
  font-weight: 600;
  color: #3f3f46;
  cursor: pointer;
}

.editor-download-project:hover {
  background: #f4f4f5;
}

.editor-main {
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  background: #fff;
}

.editor-tab-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.35rem 0.5rem 0.35rem 0.65rem;
  border-bottom: 1px solid #e4e4e7;
  background: #fff;
}

.editor-tab {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  min-width: 0;
  padding: 0.25rem 0.45rem;
  border-radius: 0.35rem;
  background: #f4f4f5;
}

.editor-tab-name {
  font-size: 0.75rem;
  font-weight: 600;
  color: #18181b;
}

.editor-tab-close {
  border: 0;
  background: transparent;
  color: #a1a1aa;
  font-size: 0.95rem;
  line-height: 1;
  cursor: pointer;
  padding: 0 0.1rem;
}

.editor-tab-download {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 2rem;
  height: 2rem;
  border: 0;
  border-radius: 0.4rem;
  background: transparent;
  color: #71717a;
  cursor: pointer;
}

.editor-tab-download:hover {
  background: #f4f4f5;
}

.editor-scroll {
  position: relative;
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  overflow: auto;
  background: #fff;
}

.editor-lines {
  padding: 0.85rem 0.65rem 0.85rem 0.85rem;
  text-align: right;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.75rem;
  line-height: 1.6;
  color: #d4d4d8;
  user-select: none;
  background: #fff;
  border-right: 1px solid #f4f4f5;
}

.editor-lines span {
  display: block;
}

.editor-code {
  margin: 0;
  padding: 0.85rem 1rem;
  border: 0;
  background: #fff;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.75rem;
  line-height: 1.6;
  color: #18181b;
  overflow: visible;
  white-space: pre;
}

.editor-code :deep(.tok-keyword) {
  color: #7c3aed;
}

.editor-code :deep(.tok-type) {
  color: #2563eb;
}

.editor-code :deep(.tok-fn) {
  color: #2563eb;
}

.editor-code :deep(.tok-string) {
  color: #059669;
}

.editor-code :deep(.tok-tag) {
  color: #d97706;
}

.editor-code :deep(.tok-name) {
  color: #ca8a04;
}

.editor-code :deep(.tok-prop) {
  color: #7c3aed;
}

.editor-code :deep(.tok-selector) {
  color: #2563eb;
}

@media (max-width: 960px) {
  .editor-workspace {
    grid-template-columns: 1fr;
  }
}
</style>
