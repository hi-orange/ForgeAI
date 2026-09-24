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
  run_check: '运行检查',
  complete_work_item: '完成当前功能',
  report_blocked: '构建暂停',
  model: '暂时无法继续',
  error: '操作失败',
  plan_files: '规划文件',
  plan_ready: '文件计划就绪',
  check_result: '检查结果',
  check_environment: '检查环境',
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
      work_item_title: event.work_item_title || legacyTitle,
      work_item_id: event.work_item_id || legacyTitle,
    })
  }
  const groups: BuildGroup[] = []
  for (const step of timelineSteps(annotated)) {
    if (step.name === 'start') continue
    let group = groups.at(-1)
    const title = step.name === 'summary' ? step.detail : step.work_item_title
    if (
      !group ||
      group.workItemId !== step.work_item_id ||
      (step.name === 'summary' && group.title !== title)
    ) {
      // A fallback work-item title can be replaced by the first actual narration.
      if (group && !group.steps.length && group.workItemId === step.work_item_id)
        group.title = title
      else {
        group = { id: step.id, title, workItemId: step.work_item_id, steps: [] }
        groups.push(group)
      }
    }
    if (step.name !== 'summary') group.steps.push(step)
  }
  return groups
}

export function isNearThreadBottom(element: {
  scrollHeight: number
  scrollTop: number
  clientHeight: number
}) {
  return element.scrollHeight - element.scrollTop - element.clientHeight < 80
}
