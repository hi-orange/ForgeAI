<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import AuthLayout from '@/layouts/AuthLayout.vue'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const form = reactive({
  username: '',
  password: '',
})
const localError = ref<string | null>(null)

async function onSubmit() {
  localError.value = null
  if (!form.username.trim() || !form.password) {
    localError.value = '请输入用户名和密码'
    return
  }

  try {
    await auth.login({
      username: form.username.trim(),
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

<template>
  <AuthLayout title="登录工作台" subtitle="用账号进入 ForgeAI，继续打磨你的智能工作流。">
    <form class="auth-form" @submit.prevent="onSubmit">
      <label>
        <span>用户名</span>
        <input v-model="form.username" type="text" autocomplete="username" placeholder="demo" />
      </label>
      <label>
        <span>密码</span>
        <input
          v-model="form.password"
          type="password"
          autocomplete="current-password"
          placeholder="••••••••"
        />
      </label>

      <p v-if="localError" class="error" role="alert">{{ localError }}</p>

      <button type="submit" class="primary" :disabled="auth.loading">
        {{ auth.loading ? '登录中…' : '登录' }}
      </button>
    </form>

    <p class="switch">
      还没有账号？
      <RouterLink :to="{ name: 'register' }">去注册</RouterLink>
    </p>
  </AuthLayout>
</template>

<style scoped>
.auth-form {
  display: grid;
  gap: 1rem;
}

label {
  display: grid;
  gap: 0.4rem;
}

label span {
  font-size: 0.85rem;
  color: var(--color-fog);
}

input {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid rgba(215, 221, 229, 0.16);
  border-radius: 0.55rem;
  background: rgba(26, 34, 44, 0.85);
  color: var(--color-mist);
  padding: 0.8rem 0.9rem;
  outline: none;
  transition:
    border-color 0.2s ease,
    box-shadow 0.2s ease;
}

input:focus {
  border-color: rgba(227, 111, 60, 0.7);
  box-shadow: 0 0 0 3px rgba(227, 111, 60, 0.18);
}

.primary {
  margin-top: 0.35rem;
  border: 0;
  border-radius: 0.55rem;
  background: linear-gradient(135deg, var(--color-ember) 0%, var(--color-ember-deep) 100%);
  color: #fff8f4;
  font-weight: 600;
  padding: 0.85rem 1rem;
  cursor: pointer;
  transition:
    transform 0.15s ease,
    filter 0.15s ease,
    opacity 0.15s ease;
}

.primary:hover:not(:disabled) {
  transform: translateY(-1px);
  filter: brightness(1.05);
}

.primary:disabled {
  opacity: 0.65;
  cursor: not-allowed;
}

.error {
  margin: 0;
  color: #ffb4a2;
  font-size: 0.9rem;
}

.switch {
  margin: 1.25rem 0 0;
  color: var(--color-fog);
  font-size: 0.92rem;
}

.switch a {
  color: #f0a57a;
  font-weight: 600;
}

.switch a:hover {
  text-decoration: underline;
}
</style>
