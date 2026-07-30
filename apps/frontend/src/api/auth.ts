import { apiRequest } from './client'

export type User = {
  id: number
  username: string
  email: string
  avatar: string | null
  created_at: string
  updated_at: string
}

export type TokenOut = {
  access_token: string
  token_type: string
}

export type RegisterPayload = {
  email: string
  password: string
}

export type LoginPayload = {
  email: string
  password: string
}

export function register(payload: RegisterPayload) {
  return apiRequest<User>('/api/v1/auth/register', {
    method: 'POST',
    body: payload,
  })
}

export function login(payload: LoginPayload) {
  return apiRequest<TokenOut>('/api/v1/auth/login', {
    method: 'POST',
    body: payload,
  })
}

export function fetchMe(token: string) {
  return apiRequest<User>('/api/v1/auth/me', {
    method: 'GET',
    token,
  })
}

export type UsernameUpdatePayload = {
  username: string
}

export function updateUsername(token: string, payload: UsernameUpdatePayload) {
  return apiRequest<User>('/api/v1/auth/username', {
    method: 'PATCH',
    token,
    body: payload,
  })
}

export type ChangePasswordPayload = {
  old_password: string
  new_password: string
}

export function changePassword(token: string, payload: ChangePasswordPayload) {
  return apiRequest<null>('/api/v1/auth/password', {
    method: 'PATCH',
    token,
    body: payload,
  })
}
