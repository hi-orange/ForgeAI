<template>
  <ProjectWorkbench v-if="useMock" />
  <RequirementsWorkbench v-else :key="String(route.params.id)" />
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'
import ProjectWorkbench from './ProjectWorkbench.vue'
import RequirementsWorkbench from './RequirementsWorkbench.vue'

const route = useRoute()
/** Demo UI only when explicitly opted in. Default is the real requirements workbench. */
const useMock = import.meta.env.VITE_PROJECT_DATA_SOURCE === 'mock'
</script>
