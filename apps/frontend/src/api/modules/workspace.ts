import { apiRequest } from '../request'

export type WorkspaceFileKind = 'text' | 'image'

export type WorkspaceFileEntry = {
  path: string
  size_bytes: number
  kind?: WorkspaceFileKind
}

export type WorkspaceListing = {
  ready: boolean
  run_id: string | null
  root: string | null
  files: WorkspaceFileEntry[]
}

export type WorkspaceFileContent = {
  path: string
  kind?: WorkspaceFileKind
  content: string
  media_type?: string | null
  truncated: boolean
  size_bytes: number
}

export function getWorkspace(projectId: number) {
  return apiRequest<WorkspaceListing>(`/api/v1/projects/${projectId}/workspace`)
}

export function getWorkspaceFile(projectId: number, path: string) {
  const query = new URLSearchParams({ path })
  return apiRequest<WorkspaceFileContent>(
    `/api/v1/projects/${projectId}/workspace/file?${query.toString()}`,
  )
}
