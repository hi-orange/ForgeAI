<template>
  <AuthLayout
    headline="自信构建 AI Agent"
    tagline="创建 · 自动化 · 扩展"
    description="面向智能工作流的现代构建平台。"
  >
    <header class="intro">
      <h2>欢迎回来</h2>
      <p>登录以继续构建智能工作流</p>
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
            :disabled="auth.loading"
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
            autocomplete="current-password"
            placeholder="请输入密码"
            :disabled="auth.loading"
          />
          <button
            type="button"
            class="eye"
            :aria-label="showPassword ? '隐藏密码' : '显示密码'"
            :disabled="auth.loading"
            @click="showPassword = !showPassword"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" />
              <circle cx="12" cy="12" r="3" />
            </svg>
          </button>
        </div>
      </label>

      <div class="row">
        <label class="remember">
          <input v-model="form.remember" type="checkbox" :disabled="auth.loading" />
          <span>记住我</span>
        </label>
        <a class="forgot" href="#" @click.prevent="onForgotPassword">忘记密码？</a>
      </div>

      <p v-if="localError" class="error" role="alert">{{ localError }}</p>

      <button type="submit" class="primary" :disabled="auth.loading">
        {{ auth.loading ? '登录中…' : '登录' }}
      </button>
    </form>

    <p class="switch">
      还没有账号？
      <RouterLink :to="{ name: 'register' }">立即注册</RouterLink>
    </p>
  </AuthLayout>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import AuthLayout from '@/layouts/AuthLayout.vue'
import { useAuthStore } from '@/stores'
import { readRememberedEmail } from '@/stores/modules/auth'

const router = useRouter()
const auth = useAuthStore()

const form = reactive({
  email: readRememberedEmail(),
  password: '',
  remember: auth.rememberMe,
})
const showPassword = ref(false)
const localError = ref<string | null>(null)

function safeRedirect(raw: unknown): string {
  if (typeof raw !== 'string') return '/'
  if (!raw.startsWith('/') || raw.startsWith('//')) return '/'
  return raw
}

function onForgotPassword() {
  localError.value = '暂未开放找回密码，请联系管理员重置'
}

async function onSubmit() {
  localError.value = null
  const email = form.email.trim()
  if (!email || !form.password) {
    localError.value = '请输入邮箱和密码'
    return
  }
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    localError.value = '请输入有效的邮箱地址'
    return
  }

  try {
    await auth.login({
      email,
      password: form.password,
      remember: form.remember,
    })
    form.password = ''
    const redirect = safeRedirect(router.currentRoute.value.query.redirect)
    await router.replace(redirect)
  } catch {
    localError.value = auth.error ?? '登录失败'
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

.eye:disabled {
  cursor: not-allowed;
  opacity: 0.6;
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

input:disabled {
  background: #f8fafc;
  cursor: not-allowed;
}

.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.remember {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  color: #475569;
  font-size: 0.9rem;
  font-weight: 500;
  cursor: pointer;
}

.remember input {
  width: 1rem;
  height: 1rem;
  accent-color: #2f6bff;
  cursor: pointer;
}

.forgot {
  color: #2f6bff;
  font-size: 0.9rem;
  font-weight: 600;
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
