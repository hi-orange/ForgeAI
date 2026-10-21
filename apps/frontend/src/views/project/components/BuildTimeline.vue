<template>
  <section class="build-timeline" aria-label="应用构建过程">
    <article v-for="(phase, phaseIndex) in phases" :key="phase.id" class="timeline-phase">
      <p class="phase-label">{{ phase.role }} · {{ phase.title }}</p>
      <p
        v-if="
          !openPhases.has(phase.id) ||
          isFailedPhase(phaseIndex) ||
          (paused && phaseIndex === phases.length - 1)
        "
        class="timeline-current"
        :class="{ failed: isFailedPhase(phaseIndex) }"
        role="status"
      >
        <span aria-hidden="true">{{ phaseMark(phaseIndex) }}</span>
        {{
          isFailedPhase(phaseIndex)
            ? '构建已暂停，需要处理'
            : paused && phaseIndex === phases.length - 1
              ? '构建已暂停，进度已保存'
              : phase.summary
        }}
      </p>
      <button
        class="timeline-toggle"
        type="button"
        :aria-expanded="openPhases.has(phase.id)"
        @click="togglePhase(phase.id)"
      >
        <span aria-hidden="true">{{ phaseMark(phaseIndex) }}</span>
        {{ isRunningPhase(phaseIndex) ? '正在处理' : '处理记录' }}
        <span>{{ openPhases.has(phase.id) ? '⌃' : '⌄' }}</span>
      </button>
      <div v-if="openPhases.has(phase.id)" class="timeline-groups">
        <article v-for="group in phase.groups" :key="group.id" class="build-group">
          <p class="group-summary">{{ group.title }}</p>
          <div
            v-if="group.steps.length"
            class="tool-card"
            :class="{ expanded: openGroups.has(groupKey(phase.id, group.id)) }"
          >
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
              <span
                v-else-if="openGroups.has(groupKey(phase.id, group.id))"
                class="tool-detail"
                :title="step.detail"
                >{{ step.detail }}</span
              >
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
                  ? '收起'
                  : '显示 ' + (group.steps.length - 1) + ' 个更多'
              }}
            </button>
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
import { buildPhases, isWriteActivity, representativeStep, type BuildGroup } from '../buildTimeline'

const props = defineProps<{
  activities: EngineeringActivity[]
  running: boolean
  failed?: boolean
  paused?: boolean
}>()
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
    if (latest) openPhases.value = new Set([latest.id])
  },
  { immediate: true },
)
function isRunningPhase(index: number) {
  return props.running && index === phases.value.length - 1
}
function isFailedPhase(index: number) {
  return props.failed && index === phases.value.length - 1
}
function phaseMark(index: number) {
  if (isRunningPhase(index)) return '◌'
  if (isFailedPhase(index)) return '!'
  return '·'
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
  const representative = representativeStep(group)
  return openGroups.value.has(groupKey(phaseId, group.id))
    ? group.steps
    : representative
      ? [representative]
      : []
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
  border-left: 1px dashed #e4e4e8;
}
.timeline-current {
  margin: 8px 0;
  color: #5d6478;
  line-height: 1.7;
  overflow-wrap: anywhere;
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
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0 8px;
  position: relative;
  padding: 8px 10px;
  border: 1px solid #e7e7eb;
  border-radius: 8px;
}
.tool-row {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px;
  padding: 5px 0;
}
.tool-card.expanded .tool-row {
  flex-basis: 100%;
}
.tool-row.failed,
.timeline-current.failed {
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
  margin-left: auto;
  border: 0;
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
