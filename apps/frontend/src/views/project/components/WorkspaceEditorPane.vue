<template>
  <section class="workspace-editor">
    <aside class="file-pane" aria-label="项目文件">
      <div class="file-search">
        <input v-model="query" type="search" placeholder="搜索文件" aria-label="搜索文件" />
      </div>
      <div class="file-tree" role="tree">
        <p v-if="!ready" class="tree-empty">计划获批并创建工作区后，这里会显示前后端文件。</p>
        <p v-else-if="loading" class="tree-empty">正在读取工作区…</p>
        <p v-else-if="error" class="tree-error">{{ error }}</p>
        <template v-else>
          <div
            v-for="node in visibleTree"
            :key="node.key"
            class="tree-row"
            :style="{ paddingLeft: `${8 + node.depth * 14}px` }"
          >
            <button
              v-if="node.kind === 'dir'"
              type="button"
              class="tree-item folder"
              @click="toggleDir(node.path)"
            >
              <WorkbenchIcon
                name="chevron-right"
                size="sm"
                :class="{ expanded: expanded.has(node.path) }"
              />
              <WorkbenchIcon name="folder" size="sm" />
              <span>{{ node.name }}</span>
            </button>
            <button
              v-else
              type="button"
              class="tree-item file"
              :class="{ active: selectedPath === node.path }"
              @click="selectFile(node.path)"
            >
              <span class="file-badge">{{ badgeFor(node.name) }}</span>
              <span>{{ node.name }}</span>
            </button>
          </div>
        </template>
      </div>
      <button type="button" class="download-btn" :disabled="!ready" @click="$emit('download')">
        <WorkbenchIcon name="download" size="sm" />
        下载项目
      </button>
    </aside>

    <div class="code-pane">
      <header class="code-tab-bar">
        <div v-if="selectedPath" class="code-tab active">
          <span class="file-badge">{{ badgeFor(selectedName) }}</span>
          <span>{{ selectedName }}</span>
        </div>
        <span v-else class="code-tab-placeholder">选择左侧文件查看源码</span>
        <button v-if="!following" type="button" class="follow-btn" @click="followWrites">
          跟随写入
        </button>
      </header>
      <div class="code-scroll">
        <p v-if="fileLoading" class="tree-empty">加载文件中…</p>
        <p v-else-if="fileError" class="tree-error">{{ fileError }}</p>
        <template v-else-if="selectedPath && fileContent !== null">
          <div class="code-lines" aria-hidden="true">
            <span v-for="line in lineCount" :key="line">{{ line }}</span>
          </div>
          <pre class="code-body"><code>{{ fileContent }}</code></pre>
        </template>
        <p v-else class="tree-empty">选择文件后可在此查看源码。</p>
      </div>
      <div class="upgrade-banner" role="note">要进行编辑，请升级到付费计划</div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useWorkspaceSource } from '../useWorkspaceSource'
import WorkbenchIcon from './WorkbenchIcon.vue'

type TreeNode =
  | { kind: 'dir'; key: string; path: string; name: string; depth: number }
  | { kind: 'file'; key: string; path: string; name: string; depth: number }

const props = defineProps<{
  projectId: number
  ready: boolean
  runId?: string | null
  generation?: number
  writtenPath?: string | null
  requestedPath?: string | null
  requestSequence?: number
}>()

defineEmits<{ download: [] }>()

const query = ref('')
const expanded = ref(new Set<string>(['frontend', 'backend', 'frontend/src', 'backend/app']))
const {
  files,
  selectedPath,
  fileContent,
  loading,
  error,
  fileLoading,
  fileError,
  following,
  selectFile,
  followWrites,
} = useWorkspaceSource(props)

const selectedName = computed(() => selectedPath.value?.split('/').at(-1) ?? '')
const lineCount = computed(() => Math.max(1, (fileContent.value ?? '').split('\n').length))

const visibleTree = computed(() => {
  const q = query.value.trim().toLowerCase()
  const paths = files.value
    .map((item) => item.path)
    .filter((path) => !q || path.toLowerCase().includes(q))
  const nodes: TreeNode[] = []
  const seenDirs = new Set<string>()

  for (const path of paths) {
    const parts = path.split('/')
    let prefix = ''
    for (let i = 0; i < parts.length; i += 1) {
      const name = parts[i]!
      const next = prefix ? `${prefix}/${name}` : name
      const depth = i
      if (i < parts.length - 1) {
        if (!seenDirs.has(next)) {
          seenDirs.add(next)
          const parent = prefix
          if (!parent || expanded.value.has(parent) || q) {
            nodes.push({ kind: 'dir', key: `d:${next}`, path: next, name, depth })
          }
        }
        if (!q && !expanded.value.has(next)) break
      } else {
        const parent = prefix
        if (!parent || expanded.value.has(parent) || q) {
          nodes.push({ kind: 'file', key: `f:${path}`, path, name, depth })
        }
      }
      prefix = next
    }
  }
  return nodes
})

function badgeFor(name: string) {
  if (name.endsWith('.vue')) return 'V'
  if (name.endsWith('.ts') || name.endsWith('.tsx')) return 'TS'
  if (name.endsWith('.py')) return 'PY'
  if (name.endsWith('.json')) return '{}'
  if (name.endsWith('.css') || name.endsWith('.scss')) return '#'
  if (name.endsWith('.html')) return '<>'
  if (name.endsWith('.md')) return 'MD'
  return '·'
}

function toggleDir(path: string) {
  const next = new Set(expanded.value)
  if (next.has(path)) next.delete(path)
  else next.add(path)
  expanded.value = next
}
</script>

<style scoped lang="scss">
.workspace-editor {
  display: grid;
  grid-template-columns: minmax(200px, 240px) minmax(0, 1fr);
  min-height: 0;
  height: 100%;
  background: #fff;
  border-left: 1px solid #ececf0;
}

.file-pane {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-right: 1px solid #ececf0;
}

.file-search {
  padding: 0.55rem 0.65rem;
  border-bottom: 1px solid #f0f0f1;
  input {
    width: 100%;
    box-sizing: border-box;
    border: 1px solid #e4e4e7;
    border-radius: 0.5rem;
    padding: 0.4rem 0.55rem;
    font-size: 0.75rem;
  }
}

.file-tree {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 0.35rem 0;
}

.tree-empty,
.tree-error,
.code-tab-placeholder {
  margin: 0.75rem;
  font-size: 0.75rem;
  color: #9193a1;
  line-height: 1.5;
}

.tree-error {
  color: #b91c1c;
}

.tree-item {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  width: 100%;
  border: 0;
  background: transparent;
  color: #3f3f46;
  font-size: 0.75rem;
  text-align: left;
  cursor: pointer;
  padding: 0.28rem 0.45rem;
  border-radius: 0.35rem;
}

.tree-item:hover,
.tree-item.active {
  background: #f4f4ff;
}

.tree-item :deep(.workbench-icon.expanded) {
  transform: rotate(90deg);
}

.file-badge {
  display: inline-flex;
  min-width: 1.25rem;
  justify-content: center;
  font-size: 0.62rem;
  font-weight: 700;
  color: #6366f1;
}

.download-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.35rem;
  margin: 0.55rem;
  border: 1px solid #e4e4e7;
  border-radius: 0.55rem;
  background: #fff;
  padding: 0.45rem 0.6rem;
  font-size: 0.75rem;
  font-weight: 600;
  cursor: pointer;
}

.download-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.code-pane {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.code-tab-bar {
  display: flex;
  align-items: center;
  min-height: 2.2rem;
  padding: 0 0.75rem;
  border-bottom: 1px solid #ececf0;
  background: #fafafa;
}
.follow-btn {
  margin-left: auto;
  border: 1px solid #e4e4e7;
  border-radius: 999px;
  background: #fff;
  padding: 0.25rem 0.55rem;
  color: #52525b;
  font-size: 0.7rem;
}

.code-tab {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.35rem 0.55rem;
  border-radius: 0.4rem 0.4rem 0 0;
  background: #fff;
  border: 1px solid #ececf0;
  border-bottom-color: #fff;
  font-size: 0.75rem;
  font-weight: 600;
}

.code-scroll {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  overflow: auto;
  background: #fff;
}

.code-lines {
  padding: 0.85rem 0.55rem;
  border-right: 1px solid #f0f0f1;
  color: #c0c2cc;
  font-size: 0.72rem;
  line-height: 1.55;
  text-align: right;
  user-select: none;
  span {
    display: block;
  }
}

.code-body {
  margin: 0;
  padding: 0.85rem 1rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.78rem;
  line-height: 1.55;
  white-space: pre;
  overflow: auto;
}

.upgrade-banner {
  position: absolute;
  left: 50%;
  bottom: 1rem;
  transform: translateX(-50%);
  padding: 0.45rem 0.85rem;
  border-radius: 999px;
  background: rgba(24, 24, 27, 0.88);
  color: #fff;
  font-size: 0.75rem;
  white-space: nowrap;
  pointer-events: none;
}
</style>
