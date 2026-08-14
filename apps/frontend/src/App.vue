<template>
  <RouterView :key="route.fullPath" />
</template>

<script setup lang="ts">
import { RouterView, useRoute } from 'vue-router'

const route = useRoute()
</script>
