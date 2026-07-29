<script setup lang="ts">
defineProps<{
  title: string
  subtitle: string
}>()
</script>

<template>
  <div class="auth-shell">
    <div class="auth-atmosphere" aria-hidden="true" />
    <div class="auth-content">
      <header class="auth-brand">
        <p class="brand-mark">ForgeAI</p>
        <h1>{{ title }}</h1>
        <p class="subtitle">{{ subtitle }}</p>
      </header>
      <slot />
    </div>
  </div>
</template>

<style scoped>
.auth-shell {
  position: relative;
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 2rem 1.25rem;
  overflow: hidden;
}

.auth-atmosphere {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse 80% 60% at 15% 20%, rgba(227, 111, 60, 0.22), transparent 55%),
    radial-gradient(ellipse 70% 50% at 85% 80%, rgba(90, 120, 150, 0.18), transparent 50%),
    linear-gradient(165deg, #0f1419 0%, #151c24 45%, #0c1015 100%);
}

.auth-atmosphere::after {
  content: '';
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(215, 221, 229, 0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(215, 221, 229, 0.03) 1px, transparent 1px);
  background-size: 48px 48px;
  mask-image: radial-gradient(circle at center, black 20%, transparent 75%);
}

.auth-content {
  position: relative;
  z-index: 1;
  width: min(100%, 26rem);
  animation: rise 0.55s ease-out both;
}

.auth-brand {
  margin-bottom: 1.75rem;
}

.brand-mark {
  margin: 0 0 0.85rem;
  font-family: var(--font-display);
  font-size: clamp(2.4rem, 6vw, 3.2rem);
  font-weight: 800;
  letter-spacing: -0.04em;
  line-height: 1;
  color: #f3f6fa;
}

.auth-brand h1 {
  margin: 0;
  font-family: var(--font-display);
  font-size: 1.35rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: #e8eef5;
}

.subtitle {
  margin: 0.45rem 0 0;
  color: var(--color-fog);
  font-size: 0.95rem;
  line-height: 1.5;
}

@keyframes rise {
  from {
    opacity: 0;
    transform: translateY(12px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
