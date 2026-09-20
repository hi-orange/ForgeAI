import { apiRequest } from '../request'

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
    auth: false,
    body: payload,
  })
}

export function login(payload: LoginPayload) {
  return apiRequest<TokenOut>('/api/v1/auth/login', {
    method: 'POST',
    auth: false,
    body: payload,
  })
}

export function fetchMe() {
  return apiRequest<User>('/api/v1/auth/me', {
    method: 'GET',
  })
}

export type UsernameUpdatePayload = {
  username: string
}

export function updateUsername(payload: UsernameUpdatePayload) {
  return apiRequest<User>('/api/v1/auth/username', {
    method: 'PATCH',
    body: payload,
  })
}

export type ChangePasswordPayload = {
  old_password: string
  new_password: string
}

export function changePassword(payload: ChangePasswordPayload) {
  return apiRequest<null>('/api/v1/auth/password', {
    method: 'PATCH',
    body: payload,
  })
}
