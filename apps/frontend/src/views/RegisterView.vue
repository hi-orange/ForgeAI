<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import AuthLayout from '@/layouts/AuthLayout.vue'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const form = reactive({
  username: '',
  email: '',
  password: '',
  confirmPassword: '',
})
const localError = ref<string | null>(null)

async function onSubmit() {
  localError.value = null

  if (!form.username.trim() || !form.email.trim() || !form.password) {
    localError.value = '请完整填写注册信息'
    return
  }
  if (form.password.length < 6) {
    localError.value = '密码至少 6 位'
    return
  }
  if (form.password !== form.confirmPassword) {
    localError.value = '两次输入的密码不一致'
    return
  }

  try {
    await auth.register({
      username: form.username.trim(),
      email: form.email.trim(),
      password: form.password,
    })
    await router.replace({ name: 'home' })
  } catch {
    localError.value = auth.error ?? '注册失败'
  }
}
</script>

<template>
  <AuthLayout title="创建账号" subtitle="注册后即可登录 ForgeAI，开始构建你的智能工作台。">
    <form class="auth-form" @submit.prevent="onSubmit">
      <label>
        <span>用户名</span>
        <input
          v-model="form.username"
          type="text"
          autocomplete="username"
          placeholder="至少 3 个字符"
        />
      </label>
      <label>
        <span>邮箱</span>
        <input
          v-model="form.email"
          type="email"
          autocomplete="email"
          placeholder="you@example.com"
        />
      </label>
      <label>
        <span>密码</span>
        <input
          v-model="form.password"
          type="password"
          autocomplete="new-password"
          placeholder="至少 6 位"
        />
      </label>
      <label>
        <span>确认密码</span>
        <input
          v-model="form.confirmPassword"
          type="password"
          autocomplete="new-password"
          placeholder="再输入一次"
        />
      </label>

      <p v-if="localError" class="error" role="alert">{{ localError }}</p>

      <button type="submit" class="primary" :disabled="auth.loading">
        {{ auth.loading ? '注册中…' : '注册并登录' }}
      </button>
    </form>

    <p class="switch">
      已有账号？
      <RouterLink :to="{ name: 'login' }">去登录</RouterLink>
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
