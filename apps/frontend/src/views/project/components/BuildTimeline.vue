<template>
  <section class="build-timeline" aria-label="应用构建过程">
    <article v-for="(phase, phaseIndex) in phases" :key="phase.id" class="timeline-phase">
      <p class="phase-label">{{ phase.role }} · {{ phase.title }}</p>
      <button
        class="timeline-toggle"
        type="button"
        :aria-expanded="openPhases.has(phase.id)"
        @click="togglePhase(phase.id)"
      >
        <span>{{ isRunningPhase(phaseIndex) ? '◌' : '✓' }}</span>
        {{ isRunningPhase(phaseIndex) ? '正在处理' : '已处理' }} {{ phase.groups.length }} 步
        <span>{{ openPhases.has(phase.id) ? '⌃' : '⌄' }}</span>
      </button>
      <p v-if="!openPhases.has(phase.id)" class="timeline-current" role="status">
        {{ phase.groups.at(-1)?.title || '正在准备工作区…' }}
      </p>
      <div v-else class="timeline-groups">
        <article v-for="group in phase.groups" :key="group.id" class="build-group">
          <p class="group-summary">{{ group.title }}</p>
          <div v-if="group.steps.length" class="tool-card">
            <div
              v-for="step in visibleSteps(phase.id, group)"
              :key="step.operation_id || step.id"
              class="tool-row"
              :class="{ failed: !step.ok }"
            >
              <span class="tool-icon" aria-hidden="true">{{
                step.ok ? (isWriteActivity(step.name) ? '✎' : '▤') : '!'
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
              :aria-expanded="openGroups.has(groupKey(phase.id, group.id))"
              @click="toggleGroup(phase.id, group.id)"
            >
              {{
                openGroups.has(groupKey(phase.id, group.id))
                  ? '隐藏'
                  : '显示 ' + (group.steps.length - 1) + ' 个更多'
              }}
            </button>
            <span
              v-if="
                group.steps.some((step) => !step.ok) &&
                !openGroups.has(groupKey(phase.id, group.id))
              "
              class="failure-note"
              >有操作未通过</span
            >
          </div>
        </article>
        <p v-if="!phase.groups.length" class="group-summary">正在准备工作区…</p>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { EngineeringActivity } from '@/api/modules/requirements'
import { buildPhases, isWriteActivity, type BuildGroup } from '../buildTimeline'

const props = defineProps<{ activities: EngineeringActivity[]; running: boolean }>()
defineEmits<{ 'open-file': [path: string] }>()
const openPhases = ref(new Set<string>())
const openGroups = ref(new Set<string>())
const phases = computed(() => buildPhases(props.activities))
const knownPhaseIds = new Set<string>()
watch(
  phases,
  (items) => {
    const fresh = items.filter((phase) => !knownPhaseIds.has(phase.id))
    items.forEach((phase) => knownPhaseIds.add(phase.id))
    const latest = fresh.at(-1)
    if (latest) openPhases.value = new Set([...openPhases.value, latest.id])
  },
  { immediate: true },
)
function isRunningPhase(index: number) {
  return props.running && index === phases.value.length - 1
}
function togglePhase(id: string) {
  const next = new Set(openPhases.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  openPhases.value = next
}
function groupKey(phaseId: string, groupId: string) {
  return `${phaseId}:${groupId}`
}
function visibleSteps(phaseId: string, group: BuildGroup) {
  return openGroups.value.has(groupKey(phaseId, group.id)) ? group.steps : group.steps.slice(0, 1)
}
function toggleGroup(phaseId: string, groupId: string) {
  const id = groupKey(phaseId, groupId)
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
.timeline-phase + .timeline-phase {
  margin-top: 18px;
}
.phase-label {
  margin: 0 0 8px;
  color: #9294a1;
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
