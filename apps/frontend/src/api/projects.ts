import { apiRequest } from './client'

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

export type ProjectStartPayload = {
  prompt?: string
}

export type ProjectStartResult = {
  project: Project
  workflow_id: string
  message: string
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

export function startProject(token: string, id: number, payload: ProjectStartPayload = {}) {
  return apiRequest<ProjectStartResult>(`/api/v1/projects/${id}/start`, {
    method: 'POST',
    token,
    body: payload,
  })
}
