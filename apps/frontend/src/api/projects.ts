import { apiRequest } from './client'

export type Project = {
  id: number
  user_id: number
  name: string
  description: string | null
  prompt: string | null
  prd: string | null
  approved_spec: string | null
  approved_at: string | null
  generated_files: string | null
  build_error: string | null
  built_at: string | null
  status: string
  created_at: string
  updated_at: string
}

export type WebsiteSection = {
  id: string
  type: string
  title: string
  description: string
  content_points: string[]
  cta: string | null
}

export type WebsitePage = {
  id: string
  name: string
  path: string
  purpose: string
  sections: WebsiteSection[]
}

export type WebsiteSpecification = {
  version: '1.0'
  product: {
    name: string
    summary: string
    target_audience: string
    primary_goal: string
  }
  site: {
    type: 'landing_page' | 'marketing_site' | 'content_site' | 'web_app'
    language: string
    pages: WebsitePage[]
  }
  design: {
    style: string
    tone: string
    primary_color: string
    accent_color: string
    font_style: string
  }
  requirements: {
    features: string[]
    integrations: string[]
    excluded: string[]
  }
  acceptance_criteria: string[]
  assumptions: string[]
}

export type GeneratedWebsiteFiles = {
  'index.html': string
  'style.css': string
  'script.js': string
}

export type ProjectCreatePayload = {
  prompt: string
  name?: string
  description?: string
}

export type ProjectStartPayload = {
  prompt?: string
}

export type SectionSelection = {
  page_id: string
  section_id: string
}

export type ProjectApproveSpecPayload = {
  selected_sections: SectionSelection[]
}

export type ProjectStartResult = {
  project: Project
  workflow_id: string
  message: string
}

export function createProject(token: string, payload: ProjectCreatePayload) {
  // Always creates a new project row; callers must not reuse by prompt/name.
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

export function approveProjectSpec(token: string, id: number, payload: ProjectApproveSpecPayload) {
  return apiRequest<Project>(`/api/v1/projects/${id}/approve-spec`, {
    method: 'POST',
    token,
    body: payload,
  })
}

export function buildProject(token: string, id: number) {
  return apiRequest<Project>(`/api/v1/projects/${id}/build`, {
    method: 'POST',
    token,
  })
}
