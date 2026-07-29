<script setup lang="ts">
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

async function onLogout() {
  auth.logout()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div class="home-shell">
    <div class="home-atmosphere" aria-hidden="true" />
    <header class="topbar">
      <p class="brand">ForgeAI</p>
      <button type="button" class="ghost" @click="onLogout">退出登录</button>
    </header>

    <main class="panel">
      <p class="eyebrow">工作台</p>
      <h1>欢迎回来，{{ auth.user?.username }}</h1>
      <p class="desc">你已成功登录。接下来可以继续接入业务模块与智能能力。</p>
      <dl>
        <div>
          <dt>邮箱</dt>
          <dd>{{ auth.user?.email }}</dd>
        </div>
        <div>
          <dt>用户 ID</dt>
          <dd>{{ auth.user?.id }}</dd>
        </div>
      </dl>
    </main>
  </div>
</template>

<style scoped>
.home-shell {
  position: relative;
  min-height: 100vh;
  padding: 1.5rem;
  overflow: hidden;
}

.home-atmosphere {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse 70% 50% at 80% 0%, rgba(227, 111, 60, 0.16), transparent 55%),
    linear-gradient(180deg, #10161d 0%, #0f1419 100%);
}

.topbar,
.panel {
  position: relative;
  z-index: 1;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 2.5rem;
}

.brand {
  margin: 0;
  font-family: var(--font-display);
  font-size: 1.55rem;
  font-weight: 800;
  letter-spacing: -0.03em;
  color: #f3f6fa;
}

.ghost {
  border: 1px solid rgba(215, 221, 229, 0.2);
  border-radius: 999px;
  background: transparent;
  color: var(--color-mist);
  padding: 0.45rem 0.95rem;
  cursor: pointer;
}

.ghost:hover {
  border-color: rgba(227, 111, 60, 0.55);
  color: #ffd7c4;
}

.panel {
  width: min(100%, 40rem);
  animation: rise 0.5s ease-out both;
}

.eyebrow {
  margin: 0 0 0.6rem;
  color: #f0a57a;
  font-size: 0.85rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

h1 {
  margin: 0;
  font-family: var(--font-display);
  font-size: clamp(1.8rem, 4vw, 2.5rem);
  font-weight: 700;
  letter-spacing: -0.03em;
  color: #f3f6fa;
}

.desc {
  margin: 0.85rem 0 1.75rem;
  color: var(--color-fog);
  line-height: 1.6;
  max-width: 34rem;
}

dl {
  display: grid;
  gap: 1rem;
  margin: 0;
}

dl div {
  display: grid;
  gap: 0.25rem;
}

dt {
  color: var(--color-fog);
  font-size: 0.82rem;
}

dd {
  margin: 0;
  color: var(--color-mist);
  font-size: 1.05rem;
}

@keyframes rise {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
