import { onBeforeUnmount, ref, watch } from 'vue'
import * as workspaceApi from '@/api/modules/workspace'

export type WorkspaceSourceInput = {
  projectId: number
  ready: boolean
  runId?: string | null
  generation?: number
  writtenPath?: string | null
  requestedPath?: string | null
  requestSequence?: number
}

export function useWorkspaceSource(input: WorkspaceSourceInput, token: () => string | null) {
  const files = ref<workspaceApi.WorkspaceFileEntry[]>([])
  const selectedPath = ref<string | null>(null)
  const fileContent = ref<string | null>(null)
  const loading = ref(false)
  const error = ref('')
  const fileLoading = ref(false)
  const fileError = ref('')
  const following = ref(true)
  let listingRequest = 0
  let fileRequest = 0

  async function selectFile(path: string, manual = true) {
    const accessToken = token()
    if (!accessToken || !input.ready) return
    if (manual) following.value = false
    const request = ++fileRequest
    const runId = input.runId
    if (selectedPath.value !== path) fileContent.value = null
    selectedPath.value = path
    fileLoading.value = fileContent.value === null
    fileError.value = ''
    try {
      const file = await workspaceApi.getWorkspaceFile(accessToken, input.projectId, path)
      if (request !== fileRequest || runId !== input.runId) return
      if (file.run_id && runId && file.run_id !== runId) return
      fileContent.value = file.content
    } catch (err) {
      if (request === fileRequest)
        fileError.value = err instanceof Error ? err.message : '读取文件失败'
    } finally {
      if (request === fileRequest) fileLoading.value = false
    }
  }

  async function loadListing(reset = false) {
    const request = ++listingRequest
    if (reset) {
      ++fileRequest
      files.value = []
      selectedPath.value = null
      fileContent.value = null
      following.value = true
      fileError.value = ''
      fileLoading.value = false
    }
    const accessToken = token()
    if (!input.ready || !accessToken) {
      loading.value = false
      return
    }
    loading.value = files.value.length === 0
    error.value = ''
    try {
      const listing = await workspaceApi.getWorkspace(accessToken, input.projectId)
      if (request !== listingRequest || (input.runId && listing.run_id !== input.runId)) return
      files.value = listing.files
      const preferred = following.value ? input.writtenPath : selectedPath.value
      const path = [
        preferred,
        selectedPath.value,
        'frontend/src/App.vue',
        listing.files[0]?.path,
      ].find((path) => path && listing.files.some((file) => file.path === path))
      if (path) await selectFile(path, false)
      else {
        ++fileRequest
        selectedPath.value = null
        fileContent.value = null
        fileLoading.value = false
      }
    } catch (err) {
      if (request === listingRequest)
        error.value = err instanceof Error ? err.message : '读取工作区失败'
    } finally {
      if (request === listingRequest) loading.value = false
    }
  }

  function followWrites() {
    following.value = true
    void loadListing()
  }
  watch(
    () => [input.projectId, input.runId, input.ready, token()] as const,
    () => {
      void loadListing(true)
    },
    { immediate: true },
  )
  watch(
    () => input.generation,
    () => {
      void loadListing()
    },
  )
  watch(
    () => [input.requestedPath, input.requestSequence] as const,
    () => {
      if (input.requestedPath) void selectFile(input.requestedPath)
    },
    { immediate: true },
  )
  onBeforeUnmount(() => {
    ++listingRequest
    ++fileRequest
  })
  return {
    files,
    selectedPath,
    fileContent,
    loading,
    error,
    fileLoading,
    fileError,
    following,
    selectFile,
    followWrites,
  }
}
