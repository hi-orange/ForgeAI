import { apiRequest } from '../request'

export type RequirementItem = {
  id: string
  text: string
}

export type AcceptanceCriterion = RequirementItem & {
  source_ids: string[]
}

export type AppSpec = {
  goal: string
  target_users: string[]
  features: RequirementItem[]
  data_requirements: RequirementItem[]
  interface_requirements: RequirementItem[]
  constraints: RequirementItem[]
  acceptance_criteria: AcceptanceCriterion[]
  open_questions: string[]
}

export type RequirementsResult = {
  project_id: number
  build_run_id: string
  cause_message_id: number
  plan_id: string
  task_id: string
  configuration_item_id: string
  outcome: 'needs_user_input' | 'awaiting_approval' | 'ready_for_delivery'
  open_questions: string[]
}

export type EngineeringActivity = {
  id: string
  name: string
  label: string
  detail: string
  ok: boolean
}

export type RequirementsStatus = {
  project_id: number
  run_id: string | null
  plan_id: string | null
  task_id: string | null
  message_id: number | null
  state:
    | 'not_started'
    | 'pending'
    | 'running'
    | 'retry_available'
    | 'stopped'
    | 'needs_user_input'
    | 'awaiting_approval'
    | 'ready_for_delivery'
    | 'design_pending'
    | 'design_running'
    | 'engineering_pending'
    | 'engineering_running'
    | 'engineering_generated'
    | 'quality_pending'
    | 'quality_running'
    | 'completed'
    | 'quality_failed'
  execution_id: string | null
  execution_expires_at: string | null
  error: string | null
  result: RequirementsResult | null
  app_spec: AppSpec | null
  workspace_ready?: boolean
  code_ready?: boolean
  code_item_id?: string | null
  test_report_item_id?: string | null
  workspace_path?: string | null
  activities?: EngineeringActivity[]
}

export type RequirementMessage = {
  id: number
  sequence: number
  sender: string
  content: string
  client_message_id: string | null
}

export type RequirementApprovalSelection = {
  id: string
  text: string
  kind: 'feature' | 'data' | 'interface' | 'constraint'
  acceptance?: string
}

export type RequirementApproval = {
  goal: string
  selected: RequirementApprovalSelection[]
  client_message_id: string
}

export function getRequirementsProject(id: number) {
  return apiRequest<{ id: number; name: string; prompt: string | null }>(`/api/v1/projects/${id}`)
}

export function getRequirements(id: number) {
  return apiRequest<RequirementsStatus>(`/api/v1/projects/${id}/requirements`)
}

export function getRequirementMessages(id: number, after = 0) {
  return apiRequest<RequirementMessage[]>(
    `/api/v1/projects/${id}/messages?limit=200&after_sequence=${after}`,
  )
}

/** Persist a turn and advance planning when the server decides it should. */
export function submitRequirements(id: number, content: string, key: string) {
  return apiRequest<RequirementsStatus>(`/api/v1/projects/${id}/requirements/submit`, {
    method: 'POST',
    body: { content, client_message_id: key },
  })
}

/** Start from an existing user message (home prompt) when still not_started. */
export function startRequirements(id: number, messageId?: number) {
  return apiRequest<RequirementsStatus>(`/api/v1/projects/${id}/requirements/start`, {
    method: 'POST',
    body: messageId != null ? { message_id: messageId } : {},
  })
}

/** Resume PM recovery or engineering from current project status. */
export function continueRequirements(id: number) {
  return apiRequest<RequirementsStatus>(`/api/v1/projects/${id}/requirements/continue`, {
    method: 'POST',
  })
}

export function pauseBuildRun(id: number, runId: string) {
  return apiRequest<RequirementsStatus>(`/api/v1/projects/${id}/build-runs/${runId}/pause`, {
    method: 'POST',
  })
}

export function approveRequirements(
  id: number,
  runId: string,
  itemId: string,
  payload: RequirementApproval,
) {
  return apiRequest<RequirementsStatus>(
    `/api/v1/projects/${id}/build-runs/${runId}/requirements/${itemId}/approval`,
    { method: 'POST', body: payload },
  )
}
