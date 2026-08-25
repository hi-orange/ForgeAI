export type PlanItem = {
  id: string
  pageId: string | null
  sectionId: string | null
  label: string
  checked: boolean
}

export type PreviewMode = 'desktop' | 'tablet' | 'mobile'
export type WorkspaceView = 'viewer' | 'overview' | 'editor' | 'files'
export type CanvasView = 'preview' | 'spec'
export type EditorFileKey = 'index.html' | 'style.css' | 'script.js'
export type EditorTab = 'visual' | 'library' | 'theme'
export type ElementRect = { x: number; y: number; width: number; height: number }

export type SelectedEditableElement = {
  elementId: string
  tagName: string
  text: string
  textEditable: boolean
  rect: ElementRect
  styles: Record<import('@/api/modules/project').EditableStyleName, string>
}

/** Design Ask thread item; tagName is display-only and not sent as bracket text. */
export type DesignChatMessage = {
  role: 'user' | 'assistant'
  content: string
  tagName?: string
}
