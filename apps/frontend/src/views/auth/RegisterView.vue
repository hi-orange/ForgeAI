<template>
  <AuthLayout
    headline="用 Forge 开始构建"
    tagline="把想法变成智能工作流。"
    description="快速上手 · 强大自动化 · 统一的 AI 运维。"
  >
    <header class="intro">
      <h2>创建账号</h2>
      <p>注册 Forge，更快构建智能工作流</p>
    </header>

    <form class="auth-form" @submit.prevent="onSubmit">
      <label>
        <span>邮箱</span>
        <div class="field">
          <span class="icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <rect x="3" y="5" width="18" height="14" rx="2" />
              <path d="m3 7 9 6 9-6" />
            </svg>
          </span>
          <input
            v-model="form.email"
            type="email"
            autocomplete="email"
            placeholder="you@example.com"
          />
        </div>
      </label>

      <label>
        <span>密码</span>
        <div class="field">
          <span class="icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <rect x="5" y="11" width="14" height="10" rx="2" />
              <path d="M8 11V8a4 4 0 0 1 8 0v3" />
            </svg>
          </span>
          <input
            v-model="form.password"
            :type="showPassword ? 'text' : 'password'"
            autocomplete="new-password"
            placeholder="请设置密码"
          />
          <button
            type="button"
            class="eye"
            :aria-label="showPassword ? '隐藏密码' : '显示密码'"
            @click="showPassword = !showPassword"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" />
              <circle cx="12" cy="12" r="3" />
            </svg>
          </button>
        </div>
      </label>

      <p v-if="localError" class="error" role="alert">{{ localError }}</p>

      <button type="submit" class="primary" :disabled="auth.loading">
        {{ auth.loading ? '创建中…' : '创建账号' }}
      </button>
    </form>

    <p class="switch">
      已有账号？
      <RouterLink :to="{ name: 'login' }">去登录</RouterLink>
    </p>
  </AuthLayout>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import AuthLayout from '@/layouts/AuthLayout.vue'
import { useAuthStore } from '@/stores'

const router = useRouter()
const auth = useAuthStore()

const form = reactive({
  email: '',
  password: '',
})
const showPassword = ref(false)
const localError = ref<string | null>(null)

async function onSubmit() {
  localError.value = null

  if (!form.email.trim() || !form.password) {
    localError.value = '请输入邮箱和密码'
    return
  }
  if (form.password.length < 6) {
    localError.value = '密码至少 6 位'
    return
  }

  try {
    await auth.register({
      email: form.email.trim(),
      password: form.password,
    })
    await router.replace({ name: 'home' })
  } catch {
    localError.value = auth.error ?? '注册失败'
  }
}
</script>

<style scoped lang="scss">
.intro {
  text-align: center;
  margin-bottom: 1.5rem;
}

.intro h2 {
  margin: 0;
  font-size: 1.65rem;
  font-weight: 700;
  letter-spacing: -0.03em;
  color: #0f172a;
}

.intro p {
  margin: 0.45rem 0 0;
  color: #64748b;
  font-size: 0.95rem;
}

.auth-form {
  display: grid;
  gap: 1rem;
}

label > span {
  display: block;
  margin-bottom: 0.4rem;
  font-size: 0.88rem;
  font-weight: 600;
  color: #334155;
}

.field {
  position: relative;
  display: flex;
  align-items: center;
}

.icon,
.eye {
  position: absolute;
  display: grid;
  place-items: center;
  width: 2.5rem;
  color: #94a3b8;
}

.icon {
  left: 0;
  pointer-events: none;
}

.eye {
  right: 0;
  border: 0;
  background: transparent;
  cursor: pointer;
}

.icon svg,
.eye svg {
  width: 1.1rem;
  height: 1.1rem;
}

input[type='email'],
input[type='password'],
input[type='text'] {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #e2e8f0;
  border-radius: 0.7rem;
  background: #fff;
  color: #0f172a;
  padding: 0.85rem 2.6rem;
  outline: none;
  transition:
    border-color 0.15s ease,
    box-shadow 0.15s ease;
}

input:focus {
  border-color: #2f6bff;
  box-shadow: 0 0 0 3px rgba(47, 107, 255, 0.15);
}

.primary {
  margin-top: 0.25rem;
  border: 0;
  border-radius: 0.75rem;
  background: #2f6bff;
  color: #fff;
  font-weight: 700;
  padding: 0.9rem 1rem;
  cursor: pointer;
  transition:
    background 0.15s ease,
    transform 0.15s ease;
}

.primary:hover:not(:disabled) {
  background: #1f54e0;
  transform: translateY(-1px);
}

.primary:disabled {
  opacity: 0.65;
  cursor: not-allowed;
}

.error {
  margin: 0;
  color: #dc2626;
  font-size: 0.9rem;
}

.switch {
  margin: 1.35rem 0 0;
  text-align: center;
  color: #64748b;
  font-size: 0.92rem;
}

.switch a {
  color: #2f6bff;
  font-weight: 700;
}
</style>
