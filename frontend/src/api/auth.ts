export interface UserInfo { email: string; name: string; is_admin?: boolean }

export async function getMe(): Promise<UserInfo | null> {
  const res = await fetch('/api/auth/me')
  if (res.status === 401) return null
  return res.json()
}

export function loginURL() { return '/api/auth/login' }
export function logoutURL() { return '/api/auth/logout' }
