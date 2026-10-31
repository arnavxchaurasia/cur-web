import { getJSON } from './http'

export interface UserInfo {
  email: string
  name: string
  picture?: string
  role?: string
  is_admin?: boolean
  is_facets_employee?: boolean
}

export interface UserProfile {
  email: string
  name?: string
  phone?: string
  company?: string
  department?: string
  time_zone?: string
  updated_at: string
}

export async function getMe(): Promise<UserInfo | null> {
  const res = await fetch('/api/auth/me')
  if (res.status === 401) return null
  return res.json()
}

export async function getUserProfile(): Promise<UserProfile> {
  return getJSON<UserProfile>('/api/user/profile')
}

export async function saveUserProfile(profile: UserProfile): Promise<UserProfile> {
  const res = await fetch('/api/user/profile', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(profile),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `save profile failed (${res.status})`)
  }
  return res.json()
}

export interface NotificationSettings {
  email_notifications: boolean
  slack_notifications: boolean
  slack_webhook_url?: string
  slack_channel?: string
}

export async function getNotificationSettings(): Promise<NotificationSettings> {
  return getJSON<NotificationSettings>('/api/user/notifications')
}

export async function updateNotificationSettings(settings: Partial<NotificationSettings>): Promise<NotificationSettings> {
  const res = await fetch('/api/user/notifications', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `update notifications failed (${res.status})`)
  }
  return res.json()
}

export interface UserTokenResponse {
  token: string
  email: string
}

export async function getUserToken(): Promise<UserTokenResponse> {
  return getJSON<UserTokenResponse>('/api/user/token')
}

export async function regenerateUserToken(): Promise<UserTokenResponse> {
  const res = await fetch('/api/user/token/regenerate', {
    method: 'POST',
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `regenerate token failed (${res.status})`)
  }
  return res.json()
}

export interface APIToken {
  key_id: string
  user_email: string
  name: string
  scopes: string[]
  created_at: string
  last_used_at?: string
  expires_at?: string
  revoked_at?: string
  display: string
}

export interface ListUserTokensResponse {
  tokens: APIToken[]
}

export interface CreateUserTokenRequest {
  name: string
  scopes?: string[]
  ttl_days?: number
}

export interface CreateUserTokenResponse {
  token: string // the full secret, returned only once
  meta: APIToken
}

export async function getUserTokens(): Promise<ListUserTokensResponse> {
  return getJSON<ListUserTokensResponse>('/api/user/tokens')
}

export async function createUserToken(
  request: CreateUserTokenRequest,
): Promise<CreateUserTokenResponse> {
  const res = await fetch('/api/user/tokens', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `create token failed (${res.status})`)
  }
  return res.json() as Promise<CreateUserTokenResponse>
}

export async function revokeUserToken(keyId: string): Promise<void> {
  const res = await fetch(`/api/user/tokens/${keyId}`, { method: 'DELETE' })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `revoke token failed (${res.status})`)
  }
}

export function loginURL() { return '/api/auth/login' }
export function logoutURL() { return '/api/auth/logout' }
