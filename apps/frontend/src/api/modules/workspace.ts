import { apiRequest } from '../request'

export type WorkspaceFileEntry = {
  path: string
  size_bytes: number
}

export type WorkspaceListing = {
  ready: boolean
  run_id: string | null
  root: string | null
  files: WorkspaceFileEntry[]
}

export type WorkspaceFileContent = {
  path: string
  content: string
  truncated: boolean
  size_bytes: number
}

export function getWorkspace(token: string, projectId: number) {
  return apiRequest<WorkspaceListing>(`/api/v1/projects/${projectId}/workspace`, { token })
}

export function getWorkspaceFile(token: string, projectId: number, path: string) {
  const query = new URLSearchParams({ path })
  return apiRequest<WorkspaceFileContent>(
    `/api/v1/projects/${projectId}/workspace/file?${query.toString()}`,
    { token },
  )
}
