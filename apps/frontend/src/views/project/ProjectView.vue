<template>
  <RequirementsWorkbench v-if="useApi" :key="String(route.params.id)" />
  <ProjectWorkbench v-else />
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'
import ProjectWorkbench from './ProjectWorkbench.vue'
import RequirementsWorkbench from './RequirementsWorkbench.vue'

const route = useRoute()
const useApi = import.meta.env.VITE_PROJECT_DATA_SOURCE === 'api'
</script>
