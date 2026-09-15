import { apiRequest } from '../request'
import * as projectMock from '@/mocks/projectPipeline'

/** Explicit opt-in only. Unset / any other value uses the real backend. */
const useProjectMock = import.meta.env.VITE_PROJECT_DATA_SOURCE === 'mock'

function demoOnlyApi<T>(action: string): Promise<T> {
  return Promise.reject(
    new Error(`${action} 仅在演示模式可用；真实流程请使用需求工作台与 BuildRun 接口`),
  )
}

/** Matches backend `ProjectOut`. Demo-only fields are optional and absent from API responses. */
export type Project = {
  id: number
  user_id: number
  name: string
  description: string | null
  prompt: string | null
  status: string
  created_at: string
  updated_at: string
  /** @deprecated Mock workbench only */
  prd?: string | null
  /** @deprecated Mock workbench only */
  approved_spec?: string | null
  /** @deprecated Mock workbench only */
  approved_at?: string | null
  /** @deprecated Mock workbench only */
  generated_files?: string | null
  /** @deprecated Mock workbench only */
  build_error?: string | null
  /** @deprecated Mock workbench only */
  validation_report?: string | null
  /** @deprecated Mock workbench only */
  website_revision?: number
  /** @deprecated Mock workbench only */
  built_at?: string | null
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

export type WebsiteElementPatch = {
  element_id: string
  changes: {
    text?: string | null
    styles?: Partial<Record<EditableStyleName, string>>
  }
}

export type EditableStyleName =
  | 'color'
  | 'background-color'
  | 'border-color'
  | 'font-size'
  | 'font-weight'
  | 'font-family'
  | 'text-align'
  | 'margin-top'
  | 'margin-right'
  | 'margin-bottom'
  | 'margin-left'
  | 'padding-top'
  | 'padding-right'
  | 'padding-bottom'
  | 'padding-left'
  | 'gap'
  | 'border-radius'

export type ProjectWebsiteEditPayload = {
  base_revision: number
  patches: WebsiteElementPatch[]
}

export type ProjectElementAiHistoryItem = {
  role: 'user' | 'assistant'
  content: string
}

export type ProjectElementAiEditPayload = {
  element_id: string
  tag_name: string
  text: string
  text_editable: boolean
  styles: Partial<Record<EditableStyleName, string>>
  instruction: string
  history?: ProjectElementAiHistoryItem[]
  base_revision: number
}

export type ProjectElementAiReply = {
  mode: 'message' | 'applied'
  message: string
  patch?: WebsiteElementPatch | null
  project?: Project | null
}

export type ProjectWebsiteReviseFocus = {
  element_id: string
  tag_name: string
  text: string
  text_editable: boolean
  styles?: Partial<Record<EditableStyleName, string>>
}

export type ProjectWebsiteRevisePayload = {
  instruction: string
  history?: ProjectElementAiHistoryItem[]
  base_revision: number
  focus?: ProjectWebsiteReviseFocus | null
}

export type ProjectWebsiteReviseReply = {
  mode: 'message' | 'applied'
  message: string
  project?: Project | null
}

export type ProjectStartResult = {
  project: Project
  workflow_id: string
  message: string
}

export function createProject(token: string, payload: ProjectCreatePayload) {
  if (useProjectMock) return Promise.resolve(projectMock.createProject(payload))

  // Always creates a new project row; callers must not reuse by prompt/name.
  return apiRequest<Project>('/api/v1/projects', {
    method: 'POST',
    token,
    body: payload,
  })
}

export function listProjects(token: string) {
  if (useProjectMock) return Promise.resolve(projectMock.listProjects())

  return apiRequest<Project[]>('/api/v1/projects', {
    method: 'GET',
    token,
  })
}

export function getProject(token: string, id: number) {
  if (useProjectMock) return Promise.resolve(projectMock.getProject(id))

  return apiRequest<Project>(`/api/v1/projects/${id}`, {
    method: 'GET',
    token,
  })
}

export function startProject(token: string, id: number, payload: ProjectStartPayload = {}) {
  if (useProjectMock) return Promise.resolve(projectMock.startProject(id, payload))

  void token
  void id
  void payload
  return demoOnlyApi<ProjectStartResult>('启动模拟规格生成')
}

export function approveProjectSpec(token: string, id: number, payload: ProjectApproveSpecPayload) {
  if (useProjectMock) return Promise.resolve(projectMock.approveProjectSpec(id, payload))

  void token
  void id
  void payload
  return demoOnlyApi<Project>('批准模拟规格')
}

export function buildProject(token: string, id: number) {
  if (useProjectMock) return Promise.resolve(projectMock.buildProject(id))

  void token
  void id
  return demoOnlyApi<Project>('模拟网站构建')
}

export function editProjectWebsite(token: string, id: number, payload: ProjectWebsiteEditPayload) {
  if (useProjectMock) return Promise.resolve(projectMock.editProjectWebsite(id, payload))

  void token
  void id
  void payload
  return demoOnlyApi<Project>('模拟网站编辑')
}

export function suggestProjectElementEdit(
  token: string,
  id: number,
  payload: ProjectElementAiEditPayload,
) {
  if (useProjectMock) return Promise.resolve(projectMock.suggestProjectElementEdit(id, payload))

  void token
  void id
  void payload
  return demoOnlyApi<ProjectElementAiReply>('模拟元素建议')
}

export function reviseProjectWebsite(
  token: string,
  id: number,
  payload: ProjectWebsiteRevisePayload,
) {
  if (useProjectMock) return Promise.resolve(projectMock.reviseProjectWebsite(id, payload))

  void token
  void id
  void payload
  return demoOnlyApi<ProjectWebsiteReviseReply>('模拟网站修订')
}
