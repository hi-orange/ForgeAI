<script setup lang="ts">
import { onMounted, ref } from 'vue'

const status = ref('checking…')
const error = ref('')

onMounted(async () => {
  try {
    const response = await fetch('/api/v1/health')
    const payload = (await response.json()) as {
      code: number
      msg: string
      data?: { status?: string; template_version?: string }
    }
    if (!response.ok || payload.code !== 0) {
      throw new Error(payload.msg || 'health check failed')
    }
    status.value = `${payload.data?.status ?? 'ok'} (${payload.data?.template_version ?? 'unknown'})`
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'failed to reach backend'
    status.value = 'unreachable'
  }
})
</script>

<template>
  <main class="page">
    <h1>Generated App</h1>
    <p>Empty fullstack-v1 template. Business features are generated from approved requirements.</p>
    <p class="status">API: {{ status }}</p>
    <p v-if="error" class="error">{{ error }}</p>
  </main>
</template>

<style scoped>
.page {
  max-width: 40rem;
  margin: 4rem auto;
  padding: 0 1.25rem;
  font-family: system-ui, sans-serif;
  color: #0f172a;
}
h1 {
  font-size: 1.75rem;
  margin-bottom: 0.75rem;
}
.status {
  margin-top: 1.5rem;
  color: #334155;
}
.error {
  color: #b91c1c;
}
</style>
