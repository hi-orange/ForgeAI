import * as projectsApi from '@/api/projects'
import type { Project, ProjectCreatePayload } from '@/api/projects'
import { defineStore } from 'pinia'
import { ref } from 'vue'

import { useAuthStore } from '@/stores/auth'

/**
 * Project identity is always `id`.
 * Home rule: each home requirement submission creates exactly one new project
 * (same prompt/name is allowed; never reuse by text).
 */
export const useProjectStore = defineStore('project', () => {
  const auth = useAuthStore()
  const items = ref<Project[]>([])
  const current = ref<Project | null>(null)
  const loading = ref(false)
  const starting = ref(false)
  const error = ref<string | null>(null)
  const workflowId = ref<string | null>(null)

  function upsertItem(project: Project) {
    // Deduplicate by id only — never by name/prompt.
    items.value = [project, ...items.value.filter((p) => p.id !== project.id)]
  }

  /** Home page: one submit => one new project, then navigate to its workbench. */
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
      const project = await projectsApi.createProject(auth.token, payload)
      current.value = project
      workflowId.value = null
      upsertItem(project)
      return project
    } catch (err) {
      error.value = err instanceof Error ? err.message : '创建项目失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  /** Run agents for an existing project id only (does not create a project). */
  async function startProject(id: number, prompt?: string) {
    if (!auth.token) {
      throw new Error('未登录或登录已过期')
    }
    starting.value = true
    error.value = null
    try {
      const started = await projectsApi.startProject(auth.token, id, prompt ? { prompt } : {})
      upsertItem(started.project)
      // Do not overwrite workbench state if the user already switched to another project.
      if (!current.value || current.value.id === id) {
        current.value = started.project
        workflowId.value = started.workflow_id
      }
      return started
    } catch (err) {
      error.value = err instanceof Error ? err.message : '启动工作流失败'
      throw err
    } finally {
      starting.value = false
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
    starting,
    error,
    workflowId,
    createFromHomeRequirement,
    startProject,
    fetchList,
    fetchOne,
  }
})
