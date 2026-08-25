import * as projectsApi from '@/api/modules/project'
import type { Project, ProjectCreatePayload } from '@/api/modules/project'
import { defineStore } from 'pinia'
import { ref } from 'vue'

import { useAuthStore } from '@/stores/modules/auth'

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
  const approving = ref(false)
  const building = ref(false)
  const savingWebsite = ref(false)
  const suggestingElement = ref(false)
  const revisingWebsite = ref(false)
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

  async function approveSpec(id: number, selectedSections: projectsApi.SectionSelection[]) {
    if (!auth.token) throw new Error('未登录或登录已过期')
    if (!selectedSections.length) throw new Error('请至少选择一个页面区块')

    approving.value = true
    error.value = null
    try {
      const approved = await projectsApi.approveProjectSpec(auth.token, id, {
        selected_sections: selectedSections,
      })
      upsertItem(approved)
      if (!current.value || current.value.id === id) current.value = approved
      return approved
    } catch (err) {
      error.value = err instanceof Error ? err.message : '批准网站规格失败'
      throw err
    } finally {
      approving.value = false
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

  async function buildProject(id: number) {
    if (!auth.token) throw new Error('未登录或登录已过期')

    building.value = true
    error.value = null
    try {
      const built = await projectsApi.buildProject(auth.token, id)
      upsertItem(built)
      if (!current.value || current.value.id === id) current.value = built
      return built
    } catch (err) {
      error.value = err instanceof Error ? err.message : '网站构建失败'
      await fetchOne(id).catch(() => undefined)
      throw err
    } finally {
      building.value = false
    }
  }

  async function editWebsite(id: number, payload: projectsApi.ProjectWebsiteEditPayload) {
    if (!auth.token) throw new Error('未登录或登录已过期')
    savingWebsite.value = true
    error.value = null
    try {
      const updated = await projectsApi.editProjectWebsite(auth.token, id, payload)
      upsertItem(updated)
      if (!current.value || current.value.id === id) current.value = updated
      return updated
    } catch (err) {
      error.value = err instanceof Error ? err.message : '保存网站修改失败'
      throw err
    } finally {
      savingWebsite.value = false
    }
  }

  async function suggestElementEdit(id: number, payload: projectsApi.ProjectElementAiEditPayload) {
    if (!auth.token) throw new Error('未登录或登录已过期')
    suggestingElement.value = true
    error.value = null
    try {
      const reply = await projectsApi.suggestProjectElementEdit(auth.token, id, payload)
      if (reply.mode === 'applied' && reply.project) {
        current.value = reply.project
        const index = items.value.findIndex((item) => item.id === reply.project!.id)
        if (index >= 0) items.value[index] = reply.project
      }
      return reply
    } catch (err) {
      error.value = err instanceof Error ? err.message : '生成元素修改建议失败'
      throw err
    } finally {
      suggestingElement.value = false
    }
  }

  async function reviseWebsite(id: number, payload: projectsApi.ProjectWebsiteRevisePayload) {
    if (!auth.token) throw new Error('未登录或登录已过期')
    revisingWebsite.value = true
    error.value = null
    try {
      const reply = await projectsApi.reviseProjectWebsite(auth.token, id, payload)
      if (reply.mode === 'applied' && reply.project) {
        upsertItem(reply.project)
        if (!current.value || current.value.id === id) current.value = reply.project
      }
      return reply
    } catch (err) {
      error.value = err instanceof Error ? err.message : '网站修改失败'
      throw err
    } finally {
      revisingWebsite.value = false
    }
  }

  return {
    items,
    current,
    loading,
    starting,
    approving,
    building,
    savingWebsite,
    suggestingElement,
    revisingWebsite,
    error,
    workflowId,
    createFromHomeRequirement,
    startProject,
    approveSpec,
    buildProject,
    editWebsite,
    suggestElementEdit,
    reviseWebsite,
    fetchList,
    fetchOne,
  }
})
