import type { Project, ProjectCreatePayload } from '@/api/modules/project'
import * as projectsApi from '@/api/modules/project'
import { defineStore } from 'pinia'
import { ref } from 'vue'

import { useAuthStore } from '@/stores/modules/auth'

export const useProjectStore = defineStore('project', () => {
  const auth = useAuthStore()
  const items = ref<Project[]>([])
  const current = ref<Project | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  function upsertItem(project: Project) {
    items.value = [project, ...items.value.filter((p) => p.id !== project.id)]
  }

  async function createFromHomeRequirement(prompt: string) {
    if (!auth.token) {
      throw new Error('未登录或登录已过期')
    }
    const text = prompt.trim()
    if (!text) {
      throw new Error('请输入需求')
    }
    loading.value = true
    error.value = null
    try {
      const payload: ProjectCreatePayload = { prompt: text }
      const project = await projectsApi.createProject(payload)
      current.value = project
      upsertItem(project)
      return project
    } catch (err) {
      error.value = err instanceof Error ? err.message : '创建项目失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  async function fetchList() {
    if (!auth.token) return
    loading.value = true
    error.value = null
    try {
      items.value = await projectsApi.listProjects()
    } catch (err) {
      error.value = err instanceof Error ? err.message : '加载项目失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  async function fetchOne(id: number) {
    if (!auth.token) {
      throw new Error('未登录或登录已过期')
    }
    loading.value = true
    error.value = null
    try {
      current.value = await projectsApi.getProject(id)
      return current.value
    } catch (err) {
      error.value = err instanceof Error ? err.message : '加载项目失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  return {
    items,
    current,
    loading,
    error,
    createFromHomeRequirement,
    fetchList,
    fetchOne,
  }
})
