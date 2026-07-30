<template>
  <AuthLayout
    headline="Build AI Agents with Confidence"
    tagline="Create. Automate. Scale."
    description="A modern platform for designing intelligent workflows."
  >
    <header class="intro">
      <h2>Welcome back</h2>
      <p>Sign in to continue building intelligent workflows</p>
    </header>

    <form class="auth-form" @submit.prevent="onSubmit">
      <label>
        <span>Email</span>
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
        <span>Password</span>
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
            placeholder="Enter your password"
          />
          <button
            type="button"
            class="eye"
            :aria-label="showPassword ? 'Hide password' : 'Show password'"
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
          <input v-model="form.remember" type="checkbox" />
          <span>Remember me</span>
        </label>
        <a class="forgot" href="#" @click.prevent>Forgot password?</a>
      </div>

      <p v-if="localError" class="error" role="alert">{{ localError }}</p>

      <button type="submit" class="primary" :disabled="auth.loading">
        {{ auth.loading ? 'Signing in…' : 'Sign In' }}
      </button>
    </form>

    <p class="switch">
      Don't have an account?
      <RouterLink :to="{ name: 'register' }">Sign up</RouterLink>
    </p>
  </AuthLayout>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import AuthLayout from '@/layouts/AuthLayout.vue'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const form = reactive({
  email: '',
  password: '',
  remember: true,
})
const showPassword = ref(false)
const localError = ref<string | null>(null)

async function onSubmit() {
  localError.value = null
  if (!form.email.trim() || !form.password) {
    localError.value = '请输入邮箱和密码'
    return
  }

  try {
    await auth.login({
      email: form.email.trim(),
      password: form.password,
    })
    const redirect =
      typeof router.currentRoute.value.query.redirect === 'string'
        ? router.currentRoute.value.query.redirect
        : '/'
    await router.replace(redirect)
  } catch {
    localError.value = auth.error ?? '登录失败'
  }
}
</script>

<style scoped>
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
}

.remember input {
  width: 1rem;
  height: 1rem;
  accent-color: #2f6bff;
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
