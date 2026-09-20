import { notifyAuthExpired } from '@/auth/session'

export type ApiResponse<T> = {
  code: number
  msg: string
  data: T
}

export class ApiError extends Error {
  code: number

  constructor(code: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.code = code
  }
}

const baseURL = import.meta.env.VITE_API_BASE_URL ?? ''

type RequestOptions = Omit<RequestInit, 'body'> & {
  body?: unknown
  /** Default true. Set false for login/register and other anonymous calls. */
  auth?: boolean
}

async function resolveAccessToken(): Promise<string | null> {
  // Lazy import avoids a static cycle: request → auth store → auth api → request.
  const { useAuthStore } = await import('@/stores/modules/auth')
  return useAuthStore().token
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, auth = true, headers, ...rest } = options
  const token = auth ? await resolveAccessToken() : null
  if (auth && !token) {
    throw new ApiError(401, '请先登录')
  }

  const response = await fetch(`${baseURL}${path}`, {
    ...rest,
    headers: {
      Accept: 'application/json',
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  if (auth && response.status === 401) {
    notifyAuthExpired()
  }

  let payload: ApiResponse<T> | null = null
  try {
    payload = (await response.json()) as ApiResponse<T>
  } catch {
    throw new ApiError(response.status, '服务器响应异常')
  }

  if (!response.ok || payload.code !== 0) {
    if (auth && response.status !== 401 && payload.code === 401) {
      notifyAuthExpired()
    }
    throw new ApiError(payload.code || response.status, payload.msg || '请求失败')
  }

  return payload.data
}
