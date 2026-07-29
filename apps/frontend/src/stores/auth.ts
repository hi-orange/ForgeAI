import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import * as authApi from '@/api/auth'
import type { LoginPayload, RegisterPayload, User } from '@/api/auth'

const TOKEN_KEY = 'forgeai_access_token'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
  const user = ref<User | null>(null)
  const bootstrapped = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const isAuthenticated = computed(() => Boolean(token.value))

  function setToken(value: string | null) {
    token.value = value
    if (value) {
      localStorage.setItem(TOKEN_KEY, value)
    } else {
      localStorage.removeItem(TOKEN_KEY)
    }
  }

  async function bootstrap() {
    if (bootstrapped.value) return
    if (!token.value) {
      bootstrapped.value = true
      return
    }

    try {
      user.value = await authApi.fetchMe(token.value)
    } catch {
      setToken(null)
      user.value = null
    } finally {
      bootstrapped.value = true
    }
  }

  async function login(payload: LoginPayload) {
    loading.value = true
    error.value = null
    try {
      const result = await authApi.login(payload)
      setToken(result.access_token)
      user.value = await authApi.fetchMe(result.access_token)
    } catch (err) {
      error.value = err instanceof Error ? err.message : '登录失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  async function register(payload: RegisterPayload) {
    loading.value = true
    error.value = null
    try {
      await authApi.register(payload)
      await login({ username: payload.username, password: payload.password })
    } catch (err) {
      error.value = err instanceof Error ? err.message : '注册失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  function logout() {
    setToken(null)
    user.value = null
    error.value = null
  }

  return {
    token,
    user,
    bootstrapped,
    loading,
    error,
    isAuthenticated,
    bootstrap,
    login,
    register,
    logout,
  }
})
