import type { EngineeringActivity } from '@/api/modules/requirements'

const labels: Record<string, string> = {
  read_artifact: '读取已批准需求',
  prepare_context: '准备项目上下文',
  editor_read: '读取必要文件',
  editor_write: '整理系统设计',
  editor_patch: '整理系统设计',
  terminal_list: '查看项目结构',
  terminal_run: '检查工程环境',
  write_system_design: '提交系统设计',
  list_files: '查看目录',
  read_file: '读取文件',
  search_code: '搜索代码',
  retrieve_code_context: '检索代码上下文',
  write_new_code: '生成文件',
  write_file: '写入文件',
  write_files: '写入文件',
  decide_action: '安排下一步工作',
  update_delivery_plan: '调整实现计划',
  generate_file: '生成文件',
  edit_file_by_replace: '修改文件',
  apply_patch: '写入文件', // 仅用于读取旧 checkpoint。
  record_engineering_memory: '记录工程约束',
  install_project_dependency: '安装项目依赖',
  install_capability: '集成通用能力',
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
  'write_files',
  'install_capability',
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
    .filter((step) => !['model', 'decide_action', 'model_response'].includes(step.name) || !step.ok)
    .map((step) => ({
      ...step,
      label: labels[step.name] || step.label,
      path:
        step.path ||
        (step.ok && (step.name === 'read_file' || writeActivities.has(step.name))
          ? activityPath(step.detail)
          : null),
      work_item_title: step.work_item_title || '',
      work_item_id: step.work_item_id || '',
    }))
}

function activityPath(detail: string): string | null {
  // Tool summaries may contain a verb and line range rather than a bare path.
  const value = detail
    ?.replace(/^(?:已)?(?:局部)?(读取|写入|修改|生成)\s+/, '')
    .replace(/(?:\s+第\s*\d+.*|（(?:完整\s*\d+\s*行|空文件)）)$/, '')
    .trim()
  return value && !/\s/.test(value) && /\.[a-z\d]+$/i.test(value) ? value : null
}

export function isWriteActivity(name: string) {
  return writeActivities.has(name)
}

/** Refresh documents for persisted plan/check changes without refetching on every read. */
export function workspaceActivityVersion(events: TimelineInput[]): string {
  return (
    [...events]
      .reverse()
      .find(
        (step) =>
          isWriteActivity(step.name) ||
          !step.ok ||
          [
            'update_delivery_plan',
            'write_system_design',
            'work_item_complete',
            'resume',
            'manual_repair_retry',
            'preview_revision_published',
            'check_result',
            'quality_result',
          ].includes(step.name) ||
          step.name.endsWith('_check_result'),
      )?.id || ''
  )
}

export type BuildGroup = {
  id: string
  title: string
  workItemId: string
  steps: TimelineStep[]
  kind: string
}

export type BuildPhase = {
  id: string
  title: string
  role: string
  summary: string
  groups: BuildGroup[]
}

/** A product-facing summary; individual tools and recovered failures stay in details. */
export function phaseSummary(events: TimelineInput[]): string {
  for (const step of [...timelineSteps(events)].reverse()) {
    if (step.name === 'resume' || step.name === 'manual_repair_retry') return '继续构建应用'
    if (step.name === 'error' || step.name === 'report_blocked') return '构建已暂停，需要处理'
    if (step.name === 'quality_error') return '验证暂时无法完成'
    if (step.name === 'quality_result') return step.ok ? '独立验证已通过' : '独立验证发现问题'
    if (step.name === 'quality_start') return '验证应用功能'
    if (step.name === 'plan_repair') return '修复检查中发现的问题'
    if (step.name === 'run_check' || (step.name.startsWith('run_') && step.name.endsWith('_check')))
      return step.ok ? '检查应用运行情况' : '工程检查发现问题'
    if (step.name === 'check_result' || step.name.endsWith('_check_result'))
      return step.ok ? '本轮工程检查已通过' : '工程检查发现问题'
    if (!step.ok) return '操作遇到问题，正在处理反馈'
    if (step.name === 'prepare_context') return '项目上下文已准备好'
    if (step.name === 'install_capability') return '已集成选用的通用能力，等待工程检查'
    if (step.name === 'work_item_complete') return `已完成：${step.detail}`
    if (step.name === 'preview_revision_published') return '新的预览版本已就绪'
    if (step.name === 'write_system_design') return '应用设计已完成'
    if (step.name === 'update_delivery_plan') return '实现计划已准备好'
    if (step.name === 'plan_files') return `实现：${step.detail}`
    if (
      writeActivities.has(step.name) ||
      step.name === 'write_files' ||
      step.name === 'generate_file'
    )
      return step.work_item_title ? `实现：${step.work_item_title}` : '编写应用功能'
  }
  if (events[0]?.phase_role === 'Architect') return '设计页面与功能结构'
  if (events[0]?.phase_role === 'Test Engineer') return '准备验证应用功能'
  return '准备应用与实现计划'
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
    'resume',
    'summary',
    'plan_files',
    'plan_repair',
    'run_check',
    'work_item_complete',
    'quality_start',
    'quality_result',
    'quality_error',
    'error',
    'report_blocked',
  ])
  const narrativeTitle = (step: TimelineStep) => {
    if (step.name === 'error' || step.name === 'report_blocked')
      return '构建暂时停住了，需要处理后继续。'
    if (step.name === 'resume') return '从已保存的进度继续构建。'
    if (step.name === 'plan_files') return `开始实现：${step.detail}`
    if (step.name === 'plan_repair') return '检查发现了一些问题，接下来进行修复。'
    if (step.name === 'run_check') return '运行检查，确认当前功能是否正常。'
    if (step.name === 'work_item_complete') return `“${step.detail}”已完成并通过工程检查。`
    if (step.name === 'quality_start') return '开始独立验证，检查应用是否满足已确认的需求。'
    if (step.name === 'quality_result')
      return step.ok ? '应用已通过独立验证。' : '独立验证发现问题，应用还需要完善。'
    if (step.name === 'quality_error') return '独立验证暂时无法完成，需要处理后继续。'
    return step.detail || step.label
  }
  const kindFor = (step: TimelineStep) => {
    if (step.name === 'install_capability') return 'capability'
    if (step.name === 'update_delivery_plan') return 'plan'
    if (['editor_patch', 'editor_write', 'write_system_design'].includes(step.name)) return 'design'
    if (
      writeActivities.has(step.name) ||
      [
        'write_files',
        'generate_file',
        'generate_api_client',
        'install_project_dependency',
      ].includes(step.name)
    )
      return 'implementation'
    if (step.name.includes('check') && step.name !== 'check_environment') return 'check'
    return 'prepare'
  }
  const titleFor = (step: TimelineStep, kind: string) => {
    if (kind === 'capability') return '集成设计中选用的通用能力，并准备检查。'
    if (kind === 'plan') return '整理功能的实现顺序，并检查依赖是否完整。'
    if (kind === 'design') return '整理页面、接口与数据结构的设计。'
    if (kind === 'check') {
      if (step.name === 'check_result' || step.name.endsWith('_check_result'))
        return step.ok ? '本轮工程检查已通过。' : '工程检查发现问题，正在分析反馈。'
      return '运行检查，确认当前功能是否正常。'
    }
    if (kind === 'implementation')
      return step.work_item_title ? `开始实现：${step.work_item_title}` : '开始编写应用功能。'
    return '准备项目上下文，了解现有结构与可复用的功能。'
  }

  const groups: BuildGroup[] = []
  let group: BuildGroup | undefined
  let feedbackPending = false
  for (const step of timelineSteps(annotated)) {
    // The generic executor startup message describes platform internals, not product progress.
    if (step.name === 'start' && step.ok) continue
    if (narrativeActivities.has(step.name)) {
      if (!step.ok) feedbackPending = true
      if (step.ok && step.name === 'work_item_complete') feedbackPending = false
      const title =
        step.name === 'run_check' && !step.ok
          ? '检查发现问题，正在分析反馈。'
          : narrativeTitle(step)
      if (group?.title === title && group.workItemId === step.work_item_id) {
        group.steps.push(step)
        continue
      }
      group = {
        id: step.id,
        title,
        workItemId: step.work_item_id,
        steps: !step.ok || step.output ? [step] : [],
        kind: 'narrative',
      }
      groups.push(group)
      continue
    }
    const kind = kindFor(step)
    const startsRepair =
      feedbackPending && step.ok && ['implementation', 'plan', 'capability'].includes(kind)
    if (
      !group ||
      group.workItemId !== step.work_item_id ||
      ((group.kind !== 'narrative' || kind === 'capability') &&
        kind !== 'prepare' &&
        group.kind !== (startsRepair ? 'repair' : kind)) ||
      (startsRepair && group.kind !== 'repair')
    ) {
      group = {
        id: step.id,
        title: startsRepair
          ? kind === 'plan'
            ? '根据校验反馈调整实现计划。'
            : '根据反馈修改应用，完成后重新检查。'
          : titleFor(step, kind),
        workItemId: step.work_item_id,
        steps: [],
        kind: startsRepair ? 'repair' : kind,
      }
      groups.push(group)
    }
    group.steps.push(step)
    if (!step.ok) feedbackPending = true
    else if (step.name === 'check_result' || step.name.endsWith('_check_result'))
      feedbackPending = false
  }
  return groups
}

/** Prefer an actionable error, otherwise one representative file, without repeating the whole batch. */
export function representativeStep(group: BuildGroup): TimelineStep | undefined {
  return (
    group.steps.find((step) => !step.ok) || group.steps.find((step) => step.path) || group.steps[0]
  )
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
    summary: phaseSummary(steps),
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
