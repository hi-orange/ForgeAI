<template>
  <section class="build-timeline" aria-label="应用构建过程">
    <button
      class="timeline-toggle"
      type="button"
      :aria-expanded="expanded"
      @click="manualExpanded = !expanded"
    >
      <span>{{ running ? '◌' : '✓' }}</span>
      {{ running ? '正在处理' : '已处理' }} {{ processedCount }} 步
      <span>{{ expanded ? '⌃' : '⌄' }}</span>
    </button>
    <p v-if="!expanded" class="timeline-current" role="status">{{ currentSummary }}</p>
    <div v-if="expanded" class="timeline-groups">
      <article v-for="group in groups" :key="group.id" class="build-group">
        <p class="group-summary">{{ group.title }}</p>
        <div v-if="group.steps.length" class="tool-card">
          <div
            v-for="step in visibleSteps(group)"
            :key="step.operation_id || step.id"
            class="tool-row"
            :class="{ failed: !step.ok }"
          >
            <span class="tool-icon" aria-hidden="true">{{
              step.ok ? (step.name === 'apply_patch' ? '✎' : '▤') : '!'
            }}</span>
            <span>{{ step.label }}</span>
            <button
              v-if="step.path"
              type="button"
              class="file-link"
              :title="step.path"
              @click="$emit('open-file', step.path)"
            >
              {{ step.path.split('/').at(-1) }}
            </button>
            <span v-else class="tool-detail" :title="step.detail">{{ step.detail }}</span>
            <details v-if="step.output || !step.ok" class="step-output">
              <summary>查看原因</summary>
              <pre>{{ step.output || step.detail }}</pre>
            </details>
          </div>
          <button
            v-if="group.steps.length > 1"
            class="more-tools"
            type="button"
            :aria-expanded="openGroups.has(group.id)"
            @click="toggleGroup(group.id)"
          >
            {{ openGroups.has(group.id) ? '隐藏' : '显示 ' + (group.steps.length - 1) + ' 个更多' }}
          </button>
          <span
            v-if="group.steps.some((step) => !step.ok) && !openGroups.has(group.id)"
            class="failure-note"
            >有操作未通过</span
          >
        </div>
      </article>
      <p v-if="!groups.length" class="group-summary">正在准备工作区…</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { EngineeringActivity } from '@/api/modules/requirements'
import { buildGroups, timelineSteps, type BuildGroup } from '../buildTimeline'

const props = defineProps<{ activities: EngineeringActivity[]; running: boolean }>()
defineEmits<{ 'open-file': [path: string] }>()
const manualExpanded = ref(false)
const expanded = computed(() => manualExpanded.value)
const openGroups = ref(new Set<string>())
const groups = computed(() => buildGroups(props.activities))
const processedCount = computed(() => timelineSteps(props.activities).length)
const currentSummary = computed(
  () => groups.value.at(-1)?.title || (props.running ? '正在准备工作区…' : '本轮处理记录已收起'),
)
function visibleSteps(group: BuildGroup) {
  return openGroups.value.has(group.id) ? group.steps : group.steps.slice(0, 1)
}
function toggleGroup(id: string) {
  const next = new Set(openGroups.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  openGroups.value = next
}
</script>

<style scoped lang="scss">
.build-timeline {
  margin: 14px 0 20px 39px;
  color: #7b7e8b;
  font-size: 12px;
}
button {
  font: inherit;
  cursor: pointer;
  color: inherit;
}
.timeline-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  border: 0;
  background: transparent;
  padding: 0;
}
.timeline-groups {
  margin: 12px 0 0 5px;
  padding-left: 16px;
  border-left: 1px solid #e4e4e8;
}
.timeline-current {
  margin: 8px 0 0 22px;
  color: #5d6478;
  line-height: 1.7;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.build-group {
  position: relative;
  margin: 0 0 20px;
}
.build-group::before {
  content: '';
  position: absolute;
  left: -20px;
  top: 7px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #e0e0e5;
}
.group-summary {
  margin: 0 0 10px;
  line-height: 1.8;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.tool-card {
  position: relative;
  padding: 8px 10px;
  border: 1px solid #e7e7eb;
  border-radius: 8px;
}
.tool-row {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px;
  padding: 5px 0;
}
.tool-row.failed,
.failure-note {
  color: #b44f4a;
}
.tool-icon {
  width: 14px;
}
.file-link {
  border: 0;
  background: none;
  padding: 0;
  color: #687c9a;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}
.file-link:hover {
  text-decoration: underline;
}
.tool-detail {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.more-tools {
  display: block;
  margin: 4px 0 0 auto;
  border: 1px solid #e7e7eb;
  border-radius: 5px;
  padding: 2px 6px;
  background: transparent;
  font-size: 11px;
}
.step-output {
  width: 100%;
}
summary {
  cursor: pointer;
}
pre {
  max-height: 180px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 11px;
}
</style>
