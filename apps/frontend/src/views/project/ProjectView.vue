<template>
  <RequirementsWorkbench :key="String(route.params.id)" />
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'
import RequirementsWorkbench from './RequirementsWorkbench.vue'

const route = useRoute()
</script>
