import { apiRequest } from '../request'

export type DeploymentStatus =
  'queued' | 'building' | 'ready' | 'failed' | 'stopped' | 'rolled_back'

export type Deployment = {
  deployment_id: string
  project_id: number
  build_run_id: string
  revision: number
  provider: string
  status: DeploymentStatus
  url: string | null
  domain: string | null
  image_reference: string | null
  secret_reference: string | null
  previous_deployment_id: string | null
  logs: string | null
  error: string | null
  created_at: string
  updated_at: string
  deployed_at: string | null
}

export function createDeployment(projectId: number) {
  return apiRequest<Deployment>(`/api/v1/projects/${projectId}/deployments`, {
    method: 'POST',
    body: { provider: 'local_docker' },
  })
}

export function listDeployments(projectId: number) {
  return apiRequest<Deployment[]>(`/api/v1/projects/${projectId}/deployments`)
}

export function getDeployment(projectId: number, deploymentId: string) {
  return apiRequest<Deployment>(`/api/v1/projects/${projectId}/deployments/${deploymentId}`)
}

export function rollbackDeployment(projectId: number, deploymentId: string) {
  return apiRequest<Deployment>(
    `/api/v1/projects/${projectId}/deployments/${deploymentId}/rollback`,
    { method: 'POST' },
  )
}
