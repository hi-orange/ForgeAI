import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { ApiError, apiRequest } from './api'

export interface AuthUser {
  id: number
  username: string
}

interface LoginResult {
  user: AuthUser
  access_token: string
  expires_at: string
}

interface AuthContextValue {
  user: AuthUser | null
  login(username: string, password: string): Promise<void>
  logout(): Promise<void>
  request<T>(path: string, init?: RequestInit): Promise<T>
}

const AuthContext = createContext<AuthContextValue | null>(null)

function authenticated<T>(token: string, path: string, init?: RequestInit): Promise<T> {
  const headers = Object.fromEntries(new Headers(init?.headers).entries())
  return apiRequest<T>(path, {
    ...init,
    headers: { ...headers, Authorization: `Bearer ${token}` },
  })
}

export function AuthProvider({ children }: { children: ReactNode }) {
  // Memory only: refresh requires sign-in; no persistent token or implicit cookie auth.
  const token = useRef<string | null>(null)
  const generation = useRef(0)
  const [user, setUser] = useState<AuthUser | null>(null)

  const logout = useCallback(async () => {
    generation.current += 1
    const previous = token.current
    token.current = null
    setUser(null)
    if (previous) await authenticated(previous, '/auth/logout', { method: 'POST' })
  }, [])

  const login = useCallback(async (username: string, password: string) => {
    const current = ++generation.current
    const previous = token.current
    token.current = null
    setUser(null)
    if (previous) await authenticated(previous, '/auth/logout', { method: 'POST' })
    const result = await apiRequest<LoginResult>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    })
    if (generation.current !== current) {
      await authenticated(result.access_token, '/auth/logout', { method: 'POST' })
      return
    }
    token.current = result.access_token
    setUser(result.user)
  }, [])

  const request = useCallback(async <T,>(path: string, init?: RequestInit): Promise<T> => {
    const current = token.current
    const currentGeneration = generation.current
    if (!current) throw new ApiError('Sign in required', 401)
    try {
      const result = await authenticated<T>(current, path, init)
      if (token.current !== current || generation.current !== currentGeneration) {
        throw new DOMException('Session changed while the request was running', 'AbortError')
      }
      return result
    } catch (error) {
      if (error instanceof ApiError && error.status === 401 && token.current === current) {
        token.current = null
        setUser(null)
      }
      throw error
    }
  }, [])

  const value = useMemo(() => ({ user, login, logout, request }), [user, login, logout, request])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const auth = useContext(AuthContext)
  if (!auth) throw new Error('useAuth must be used within AuthProvider')
  return auth
}
