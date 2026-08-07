import * as projectsApi from '@/api/projects'
import type { Project, ProjectCreatePayload } from '@/api/projects'
import { defineStore } from 'pinia'
import { ref } from 'vue'

import { useAuthStore } from '@/stores/auth'

export const useProjectStore = defineStore('project', () => {
  const auth = useAuthStore()
  const items = ref<Project[]>([])
  const current = ref<Project | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function createAndStart(prompt: string) {
    if (!auth.token) {
      throw new Error('未登录或登录已过期')
    }
    loading.value = true
    error.value = null
    try {
      const payload: ProjectCreatePayload = { prompt }
      const project = await projectsApi.createProject(auth.token, payload)
      const started = await projectsApi.startProject(auth.token, project.id, { prompt })
      current.value = started.project
      items.value = [started.project, ...items.value.filter((p) => p.id !== started.project.id)]
      return started
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
      items.value = await projectsApi.listProjects(auth.token)
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
      current.value = await projectsApi.getProject(auth.token, id)
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
    createAndStart,
    fetchList,
    fetchOne,
  }
})
