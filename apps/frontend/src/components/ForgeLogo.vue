<template>
  <span class="forge-logo" :style="{ width: `${size}px`, height: `${size}px` }" aria-hidden="true">
    <img :src="markUrl" alt="" width="40" height="40" draggable="false" />
  </span>
</template>

<script setup lang="ts">
import markUrl from '@/assets/brand/forge-mark.png'

withDefaults(
  defineProps<{
    size?: number
  }>(),
  { size: 28 },
)
</script>

<style scoped>
.forge-logo {
  display: inline-grid;
  place-items: center;
  flex-shrink: 0;
}

.forge-logo img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  display: block;
}
</style>
