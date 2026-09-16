import { apiRequest } from '../request'

/** Matches backend `ProjectOut`. */
export type Project = {
  id: number
  user_id: number
  name: string
  description: string | null
  prompt: string | null
  status: string
  created_at: string
  updated_at: string
}

export type ProjectCreatePayload = {
  prompt: string
  name?: string
  description?: string
}

export function createProject(token: string, payload: ProjectCreatePayload) {
  return apiRequest<Project>('/api/v1/projects', {
    method: 'POST',
    token,
    body: payload,
  })
}

export function listProjects(token: string) {
  return apiRequest<Project[]>('/api/v1/projects', {
    method: 'GET',
    token,
  })
}

export function getProject(token: string, id: number) {
  return apiRequest<Project>(`/api/v1/projects/${id}`, {
    method: 'GET',
    token,
  })
}
