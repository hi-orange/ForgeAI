import type { EngineeringActivity } from '@/api/modules/requirements'

const labels: Record<string, string> = {
  read_artifact: '读取已批准需求',
  editor_read: '读取必要文件',
  editor_write: '整理系统设计',
  terminal_list: '查看项目结构',
  terminal_run: '检查工程环境',
  write_system_design: '提交系统设计',
  list_files: '查看目录',
  read_file: '读取文件',
  search_code: '搜索代码',
  retrieve_code_context: '检索代码上下文',
  write_new_code: '生成文件',
  write_file: '写入文件',
  generate_file: '生成文件',
  edit_file_by_replace: '修改文件',
  apply_patch: '写入文件', // 仅用于读取旧 checkpoint。
  record_engineering_memory: '记录工程约束',
  install_project_dependency: '安装项目依赖',
  run_check: '运行检查',
  complete_work_item: '完成当前功能',
  report_blocked: '构建暂停',
  model: '暂时无法继续',
  error: '操作失败',
  plan_files: '规划文件',
  plan_ready: '文件计划就绪',
  check_result: '检查结果',
  check_environment: '检查环境',
  work_item_complete: '完成当前功能',
  plan_repair: '规划修复',
  quality_start: '开始独立验证',
  quality_result: '独立验证完成',
  quality_error: '独立验证失败',
  resume: '继续构建',
}

const roleLabels: Record<string, string> = {
  Architect: '架构师',
  'Code Engineer': '工程师',
  'Test Engineer': '测试工程师',
}

const writeActivities = new Set([
  'write_new_code',
  'write_file',
  'edit_file_by_replace',
  'apply_patch',
])

/** Optional presentation fields beyond the API `EngineeringActivity` contract. */
export type TimelineInput = EngineeringActivity & {
  operation_id?: string | null
  status?: string | null
  output?: string | null
  path?: string | null
  work_item_title?: string | null
  work_item_id?: string | null
}

/** Presentation row derived from API activity (plus optional extras). */
export type TimelineStep = EngineeringActivity & {
  operation_id?: string | null
  status?: string | null
  output?: string | null
  path: string | null
  work_item_title: string
  work_item_id: string
}

export function timelineSteps(events: TimelineInput[]): TimelineStep[] {
  const rows = new Map<string, TimelineInput>()
  for (const event of events) {
    const key = event.operation_id || event.id
    rows.set(key, { ...rows.get(key), ...event })
  }
  return [...rows.values()]
    .filter((step) => step.name !== 'model' || !step.ok)
    .map((step) => ({
      ...step,
      label: labels[step.name] || step.label,
      path:
        step.path ||
        (step.ok && (step.name === 'read_file' || writeActivities.has(step.name))
          ? step.detail || null
          : null),
      work_item_title: step.work_item_title || '',
      work_item_id: step.work_item_id || '',
    }))
}

export function isWriteActivity(name: string) {
  return writeActivities.has(name)
}

export type BuildGroup = {
  id: string
  title: string
  workItemId: string
  steps: TimelineStep[]
}

export type BuildPhase = {
  id: string
  title: string
  role: string
  groups: BuildGroup[]
}

function compactToolSteps(steps: TimelineStep[]) {
  if (steps.length <= 4) return steps
  const candidates = [
    steps[0],
    ...steps.filter((step) => !step.ok),
    [...steps].reverse().find((step) => isWriteActivity(step.name)),
    steps.at(-1),
  ].filter((step): step is TimelineStep => Boolean(step))
  const unique = new Map(candidates.map((step) => [step.operation_id || step.id, step]))
  return [...unique.values()].slice(0, 4)
}

/** Narration starts a step; consecutive tools share its collapsed card.
 * Old checkpoints without narration group by work item instead of repeating model calls.
 */
export function buildGroups(events: TimelineInput[]): BuildGroup[] {
  const annotated: TimelineStep[] = []
  let legacyTitle = '正在构建应用'
  for (const event of events) {
    if (['start', 'model'].includes(event.name) && event.ok && event.detail)
      legacyTitle = event.detail
    annotated.push({
      ...event,
      path: event.path ?? null,
      work_item_title: event.work_item_title || event.phase_label || legacyTitle,
      work_item_id: event.work_item_id || event.phase_id || legacyTitle,
    })
  }
  const narrativeActivities = new Set([
    'start',
    'resume',
    'summary',
    'plan_files',
    'plan_repair',
    'run_check',
    'work_item_complete',
    'quality_start',
    'quality_result',
    'quality_error',
  ])
  const narrativeTitle = (step: TimelineStep) => {
    if (step.name === 'plan_files') return `开始实现：${step.detail}`
    if (step.name === 'plan_repair') return `${step.detail}，只修复检查发现的问题。`
    if (step.name === 'run_check') return step.detail
    if (step.name === 'work_item_complete') return `“${step.detail}”已完成并通过工程检查。`
    if (step.name === 'quality_start') return '业务代码已冻结，开始独立验证。'
    if (step.name === 'quality_result')
      return step.ok ? `独立验证已完成：${step.detail}` : `独立验证发现问题：${step.detail}`
    if (step.name === 'quality_error') return `独立验证执行失败：${step.detail}`
    return step.detail || step.label
  }
  const continuationTitle = (step: TimelineStep) => {
    const target = (step.path || step.detail).split('（')[0]
    if (target && ['generate_file', 'write_file', 'read_file'].includes(step.name))
      return `继续处理 ${target}`
    return `继续实现：${step.work_item_title}`
  }

  const groups: BuildGroup[] = []
  let group: BuildGroup | undefined
  for (const step of timelineSteps(annotated)) {
    if (narrativeActivities.has(step.name)) {
      group = {
        id: step.id,
        title: narrativeTitle(step),
        workItemId: step.work_item_id,
        steps: [],
      }
      groups.push(group)
      continue
    }
    if (!group || group.workItemId !== step.work_item_id) {
      group = {
        id: step.id,
        title: group ? continuationTitle(step) : step.work_item_title,
        workItemId: step.work_item_id,
        steps: [],
      }
      groups.push(group)
    }
    group.steps.push(step)
  }
  return groups.map((item) => ({ ...item, steps: compactToolSteps(item.steps) }))
}

/** Retries of one role assignment stay in one conversational engineering turn. */
export function buildPhases(events: TimelineInput[]): BuildPhase[] {
  const phases = new Map<string, TimelineInput[]>()
  for (const event of events) {
    const phaseId = event.phase_id || 'legacy'
    const phase = phases.get(phaseId) || []
    phase.push(event)
    phases.set(phaseId, phase)
  }
  return [...phases.entries()].map(([id, steps]) => ({
    id,
    title: steps[0]?.phase_label || '构建应用',
    role: roleLabels[steps[0]?.phase_role || ''] || steps[0]?.phase_role || '工程师',
    groups: buildGroups(steps),
  }))
}

export function isNearThreadBottom(element: {
  scrollHeight: number
  scrollTop: number
  clientHeight: number
}) {
  return element.scrollHeight - element.scrollTop - element.clientHeight < 80
}
