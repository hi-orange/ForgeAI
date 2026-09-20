import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import * as authApi from '@/api/modules/auth'
import type {
  ChangePasswordPayload,
  LoginPayload,
  RegisterPayload,
  User,
  UsernameUpdatePayload,
} from '@/api/modules/auth'

const TOKEN_KEY = 'forgeai_access_token'
const REMEMBER_KEY = 'forgeai_remember_me'
const EMAIL_KEY = 'forgeai_remembered_email'

function readStoredToken(): string | null {
  return localStorage.getItem(TOKEN_KEY) ?? sessionStorage.getItem(TOKEN_KEY)
}

function readRememberPreference(): boolean {
  const raw = localStorage.getItem(REMEMBER_KEY)
  if (raw === null) return true
  return raw === '1'
}

export function readRememberedEmail(): string {
  return localStorage.getItem(EMAIL_KEY) ?? ''
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(readStoredToken())
  const user = ref<User | null>(null)
  const bootstrapped = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const rememberMe = ref(readRememberPreference())

  const isAuthenticated = computed(() => Boolean(token.value))

  function setToken(value: string | null, remember: boolean = rememberMe.value) {
    token.value = value
    rememberMe.value = remember
    localStorage.setItem(REMEMBER_KEY, remember ? '1' : '0')

    localStorage.removeItem(TOKEN_KEY)
    sessionStorage.removeItem(TOKEN_KEY)

    if (!value) return

    if (remember) {
      localStorage.setItem(TOKEN_KEY, value)
    } else {
      sessionStorage.setItem(TOKEN_KEY, value)
    }
  }

  function setRememberedEmail(email: string | null) {
    if (email) {
      localStorage.setItem(EMAIL_KEY, email)
    } else {
      localStorage.removeItem(EMAIL_KEY)
    }
  }

  async function bootstrap() {
    if (bootstrapped.value) return
    if (!token.value) {
      bootstrapped.value = true
      return
    }

    try {
      user.value = await authApi.fetchMe()
    } catch {
      setToken(null)
      user.value = null
    } finally {
      bootstrapped.value = true
    }
  }

  async function login(payload: LoginPayload & { remember?: boolean }) {
    loading.value = true
    error.value = null
    const remember = payload.remember ?? true
    try {
      const result = await authApi.login({
        email: payload.email,
        password: payload.password,
      })
      setToken(result.access_token, remember)
      if (remember) {
        setRememberedEmail(payload.email)
      } else {
        setRememberedEmail(null)
      }
      user.value = await authApi.fetchMe()
    } catch (err) {
      error.value = err instanceof Error ? err.message : '登录失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  async function register(payload: RegisterPayload & { remember?: boolean }) {
    loading.value = true
    error.value = null
    const remember = payload.remember ?? true
    try {
      await authApi.register(payload)
      const result = await authApi.login({
        email: payload.email,
        password: payload.password,
      })
      setToken(result.access_token, remember)
      if (remember) {
        setRememberedEmail(payload.email)
      } else {
        setRememberedEmail(null)
      }
      user.value = await authApi.fetchMe()
    } catch (err) {
      error.value = err instanceof Error ? err.message : '注册失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  async function updateUsername(payload: UsernameUpdatePayload) {
    if (!token.value) {
      throw new Error('未登录或登录已过期')
    }
    loading.value = true
    error.value = null
    try {
      user.value = await authApi.updateUsername(payload)
      return user.value
    } catch (err) {
      error.value = err instanceof Error ? err.message : '修改用户名失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  async function changePassword(payload: ChangePasswordPayload) {
    if (!token.value) {
      throw new Error('未登录或登录已过期')
    }
    loading.value = true
    error.value = null
    try {
      await authApi.changePassword(payload)
      logout()
    } catch (err) {
      error.value = err instanceof Error ? err.message : '修改密码失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  function logout() {
    setToken(null, rememberMe.value)
    user.value = null
    error.value = null
  }

  return {
    token,
    user,
    bootstrapped,
    loading,
    error,
    rememberMe,
    isAuthenticated,
    bootstrap,
    login,
    register,
    updateUsername,
    changePassword,
    logout,
  }
})
