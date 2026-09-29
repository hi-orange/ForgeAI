export interface ApiEnvelope<T> {
  code: number
  msg: string
  data: T
}

export interface HealthStatus {
  status: string
  template_version: string
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: number,
    readonly detail: string = message,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })

  let payload: ApiEnvelope<T>
  try {
    payload = (await response.json()) as ApiEnvelope<T>
  } catch {
    throw new ApiError(`Request failed with HTTP ${response.status}`, response.status)
  }

  if (!response.ok || payload.code !== 0) {
    throw new ApiError(payload.msg || 'Request failed', response.status, payload.code)
  }

  return payload.data
}

export function getHealth(): Promise<HealthStatus> {
  return apiRequest<HealthStatus>('/health')
}
