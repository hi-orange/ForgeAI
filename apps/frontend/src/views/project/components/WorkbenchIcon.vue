<template>
  <svg
    class="workbench-icon"
    :class="sizeClass"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <template v-if="name === 'history'">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
      <path d="M8 2.5 5.5 5" />
    </template>
    <template v-else-if="name === 'chat-collapse'">
      <path d="m11 8-4 4 4 4" />
      <path d="m17 8-4 4 4 4" />
    </template>
    <template v-else-if="name === 'chat-expand'">
      <path d="m7 8 4 4-4 4" />
      <path d="m13 8 4 4-4 4" />
    </template>
    <template v-else-if="name === 'editor-code'">
      <path d="m16 18 6-6-6-6" />
      <path d="m8 6-6 6 6 6" />
    </template>
    <template v-else-if="name === 'cloud-upload'">
      <path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10Z" />
      <path d="M12 15V9" />
      <path d="m9 12 3-3 3 3" />
    </template>
    <template v-else-if="name === 'folder'">
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" />
    </template>
    <template v-else-if="name === 'more-dots'">
      <circle cx="5" cy="12" r="1.25" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1.25" fill="currentColor" stroke="none" />
      <circle cx="19" cy="12" r="1.25" fill="currentColor" stroke="none" />
    </template>
    <template v-else-if="name === 'refresh'">
      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
      <path d="M21 3.5V9H15.5" />
    </template>
    <template v-else-if="name === 'paintbrush'">
      <path d="M18.37 3.63a2.12 2.12 0 0 1 3 3L8.5 21.5l-4 1 1-4Z" />
      <path d="m15 6 3 3" />
    </template>
    <template v-else-if="name === 'console'">
      <path d="m7 8 3 3-3 3" />
      <path d="m13 14 3-3-3-3" />
    </template>
    <template v-else-if="name === 'check'">
      <path d="m5 12 4 4 10-10" />
    </template>
    <template v-else-if="name === 'chevron-down'">
      <path d="m6 9 6 6 6-6" />
    </template>
    <template v-else-if="name === 'chevron-right'">
      <path d="m9 6 6 6-6 6" />
    </template>
    <template v-else-if="name === 'download'">
      <path d="M12 3v10" />
      <path d="m8 9 4 4 4-4" />
      <path d="M5 20h14" />
    </template>
    <template v-else-if="name === 'home'">
      <path d="m4 10.5 8-6.5 8 6.5" />
      <path d="M6 9.5V20h12V9.5" />
    </template>
    <template v-else-if="name === 'desktop'">
      <rect x="3" y="4" width="18" height="12" rx="1.5" />
      <path d="M8 20h8M12 16v4" />
    </template>
    <template v-else-if="name === 'tablet'">
      <rect x="5" y="2.5" width="14" height="19" rx="2" />
      <path d="M10 18.5h4" />
    </template>
    <template v-else-if="name === 'mobile'">
      <rect x="7" y="2.5" width="10" height="19" rx="2" />
      <path d="M11 18.5h2" />
    </template>
    <template v-else-if="name === 'external-link'">
      <path d="M14 4h6v6" />
      <path d="m20 4-9 9" />
      <path d="M18 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h6" />
    </template>
  </svg>
</template>

<script setup lang="ts">
import { computed } from 'vue'

export type WorkbenchIconName =
  | 'history'
  | 'chat-collapse'
  | 'chat-expand'
  | 'editor-code'
  | 'cloud-upload'
  | 'folder'
  | 'more-dots'
  | 'refresh'
  | 'paintbrush'
  | 'console'
  | 'check'
  | 'chevron-down'
  | 'chevron-right'
  | 'download'
  | 'home'
  | 'desktop'
  | 'tablet'
  | 'mobile'
  | 'external-link'

const props = withDefaults(
  defineProps<{
    name: WorkbenchIconName
    size?: 'sm' | 'md'
    strokeWidth?: number
  }>(),
  {
    size: 'sm',
    strokeWidth: 1.75,
  },
)

const sizeClass = computed(() => (props.size === 'sm' ? 'is-sm' : 'is-md'))
</script>

<style scoped>
.workbench-icon {
  flex-shrink: 0;
}

.workbench-icon.is-md {
  width: 1.25rem;
  height: 1.25rem;
}

.workbench-icon.is-sm {
  width: 1.125rem;
  height: 1.125rem;
}
</style>
