import { apiRequest } from '../request'

export type PreviewState = 'idle' | 'starting' | 'ready' | 'error' | 'disabled'

export type PreviewStatus = {
  status: PreviewState
  url: string | null
  run_id: string | null
  message: string | null
}

export function getPreview(projectId: number) {
  return apiRequest<PreviewStatus>(`/api/v1/projects/${projectId}/preview`)
}

export function startPreview(projectId: number) {
  return apiRequest<PreviewStatus>(`/api/v1/projects/${projectId}/preview/start`, {
    method: 'POST',
  })
}

export function stopPreview(projectId: number) {
  return apiRequest<PreviewStatus>(`/api/v1/projects/${projectId}/preview/stop`, {
    method: 'POST',
  })
}
