import { apiRequest } from '../request'

export type PreviewState = 'idle' | 'starting' | 'ready' | 'error' | 'disabled'

export type PreviewStatus = {
  status: PreviewState
  url: string | null
  run_id: string | null
  message: string | null
}

export type PreviewTheme = {
  preset_id: string
  name: string
  background: string
  surface: string
  text: string
  primary: string
  muted: string
  border: string
  font_family: string
  radius: number
  shadow: string
}

export type PreviewElementStyles = Partial<{
  margin: string
  padding: string
  backgroundColor: string
  color: string
  opacity: string
  borderRadius: string
  fontFamily: string
  fontWeight: string
  fontSize: string
  textAlign: string
  textDecoration: string
}>

export type PreviewElementOverride = {
  selector: string
  label: string
  styles: PreviewElementStyles
  text: string | null
  image_url: string | null
}

export type PreviewDesignState = {
  revision: number
  saved_at: string | null
  theme: PreviewTheme
  elements: PreviewElementOverride[]
}

export type PreviewSelection = PreviewElementOverride & {
  tag: string
  class_name: string
  rect: { width: number; height: number }
}

export type PreviewPage = { path: string; label: string }

export type PreviewConsoleRow = {
  id: string
  level: 'log' | 'info' | 'warn' | 'error'
  message: string
  source: 'page' | 'design'
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

export function getPreviewDesign(projectId: number) {
  return apiRequest<PreviewDesignState>(`/api/v1/projects/${projectId}/preview/design`)
}

export function savePreviewDesign(
  projectId: number,
  state: Pick<PreviewDesignState, 'theme' | 'elements'>,
) {
  return apiRequest<PreviewDesignState>(`/api/v1/projects/${projectId}/preview/design`, {
    method: 'PUT',
    body: state,
  })
}
