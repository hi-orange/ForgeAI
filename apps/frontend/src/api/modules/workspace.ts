import { apiRequest } from '../request'

export type WorkspaceFileKind = 'text' | 'image' | 'document'

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
  run_id?: string | null
  kind?: WorkspaceFileKind
  content: string
  media_type?: string | null
  truncated: boolean
  size_bytes: number
}

export function getWorkspace(projectId: number, runId?: string | null) {
  const query = runId ? `?${new URLSearchParams({ run_id: runId })}` : ''
  return apiRequest<WorkspaceListing>(`/api/v1/projects/${projectId}/workspace${query}`)
}

export function getWorkspaceFile(projectId: number, path: string, runId?: string | null) {
  const query = new URLSearchParams({ path })
  if (runId) query.set('run_id', runId)
  return apiRequest<WorkspaceFileContent>(
    `/api/v1/projects/${projectId}/workspace/file?${query.toString()}`,
  )
}
