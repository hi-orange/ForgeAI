import { apiRequest } from '../request'

export type AppSpec = {
  goal: string
  target_users: string[]
  features: string[]
  data_requirements: string[]
  interface_requirements: string[]
  constraints: string[]
  acceptance_criteria: string[]
  open_questions: string[]
}

export type RequirementsResult = {
  project_id: number
  build_run_id: string
  cause_message_id: number
  plan_id: string
  task_id: string
  configuration_item_id: string
  outcome: 'needs_user_input' | 'ready_for_design'
  open_questions: string[]
  design_plan_id: string | null
  design_task_id: string | null
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
    | 'ready_for_design'
    | 'design_pending'
  execution_id: string | null
  execution_expires_at: string | null
  error: string | null
  result: RequirementsResult | null
  app_spec: AppSpec | null
}

export type RequirementMessage = {
  id: number
  sequence: number
  sender: string
  content: string
  client_message_id: string | null
}

export function getRequirementsProject(token: string, id: number) {
  return apiRequest<{ id: number; name: string; prompt: string | null }>(`/api/v1/projects/${id}`, {
    token,
  })
}

export function getRequirements(token: string, id: number) {
  return apiRequest<RequirementsStatus>(`/api/v1/projects/${id}/requirements`, { token })
}

export function getRequirementMessages(token: string, id: number, after = 0) {
  return apiRequest<RequirementMessage[]>(
    `/api/v1/projects/${id}/messages?limit=200&after_sequence=${after}`,
    { token },
  )
}

export function createRequirementMessage(token: string, id: number, content: string, key: string) {
  return apiRequest<RequirementMessage>(`/api/v1/projects/${id}/messages`, {
    method: 'POST',
    token,
    body: { content, client_message_id: key },
  })
}

export function classifyRequirementMessage(token: string, id: number, messageId: number) {
  return apiRequest<{ category: string }>(
    `/api/v1/projects/${id}/messages/${messageId}/classification`,
    {
      method: 'POST',
      token,
    },
  )
}

export function createRequirementsRun(token: string, id: number) {
  return apiRequest<{ run_id: string }>(`/api/v1/projects/${id}/build-runs`, {
    method: 'POST',
    token,
  })
}

export function executeRequirements(
  token: string,
  id: number,
  runId: string,
  messageId: number,
  recoveryId: string | null = null,
) {
  return apiRequest<RequirementsResult>(`/api/v1/projects/${id}/build-runs/${runId}/requirements`, {
    method: 'POST',
    token,
    body: { message_id: messageId, recovery_execution_id: recoveryId },
  })
}

export function answerRequirements(
  token: string,
  id: number,
  runId: string,
  itemId: string,
  content: string,
  key: string,
) {
  return apiRequest<RequirementsResult>(
    `/api/v1/projects/${id}/build-runs/${runId}/requirements/${itemId}/answers`,
    { method: 'POST', token, body: { content, client_message_id: key } },
  )
}
