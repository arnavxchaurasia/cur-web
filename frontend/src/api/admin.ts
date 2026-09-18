// frontend/src/api/admin.ts
import type { Job } from './jobs'

export interface PagedJobs {
  jobs: Job[]
  total: number
  limit: number
  offset: number
}

export interface ListJobsParams {
  limit?: number
  offset?: number
  status?: string
  from?: string // yyyy-mm-dd
  to?: string   // yyyy-mm-dd
}

export async function listAllJobsPaged(params: ListJobsParams = {}): Promise<PagedJobs> {
  const qs = new URLSearchParams()
  if (params.limit != null) qs.set('limit', String(params.limit))
  if (params.offset != null) qs.set('offset', String(params.offset))
  if (params.status) qs.set('status', params.status)
  if (params.from) qs.set('from', params.from)
  if (params.to) qs.set('to', params.to)
  const res = await fetch(`/api/admin/jobs?${qs.toString()}`)
  if (!res.ok) return { jobs: [], total: 0, limit: params.limit ?? 50, offset: params.offset ?? 0 }
  return res.json()
}

export async function cancelJob(id: string): Promise<void> {
  const res = await fetch(`/api/admin/jobs/${id}/cancel`, { method: 'POST' })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `cancel failed (${res.status})`)
  }
}

export async function deleteJob(id: string): Promise<void> {
  const res = await fetch(`/api/admin/jobs/${id}`, { method: 'DELETE' })
  if (!res.ok && res.status !== 204) {
    const text = await res.text()
    throw new Error(text || `delete failed (${res.status})`)
  }
}

export interface AdminUser {
  email: string
  role: string
  created_at: string
  updated_at: string
}

export async function listUsers(): Promise<AdminUser[]> {
  const res = await fetch('/api/admin/users')
  if (!res.ok) return []
  return res.json()
}

export async function setUserRole(email: string, role: string): Promise<void> {
  const res = await fetch(`/api/admin/users/${encodeURIComponent(email)}/role`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role }),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `role update failed (${res.status})`)
  }
}

export interface CatalogStatus {
  age_days: number
  stale: boolean
}

export async function getCatalogStatus(): Promise<CatalogStatus | null> {
  const res = await fetch('/api/admin/catalog/status')
  if (!res.ok) return null
  return res.json()
}

export async function triggerCatalogRefresh(): Promise<void> {
  const res = await fetch('/api/admin/catalog/refresh', { method: 'POST' })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `refresh failed (${res.status})`)
  }
}

export interface AuditEntry {
  id: number
  actor_email: string
  action: string
  target_id: string
  details: string
  created_at: string
}

export async function listAuditLog(params: { limit?: number; offset?: number } = {}): Promise<AuditEntry[]> {
  const qs = new URLSearchParams()
  if (params.limit != null) qs.set('limit', String(params.limit))
  if (params.offset != null) qs.set('offset', String(params.offset))
  const res = await fetch(`/api/admin/audit?${qs.toString()}`)
  if (!res.ok) return []
  return res.json()
}

export interface DayCount {
  day: string
  count: number
}

export interface UsageStats {
  total_jobs: number
  done_jobs: number
  failed_jobs: number
  failure_rate: number
  avg_processing_seconds: number
  jobs_by_day: DayCount[] | null
}

export async function getAnalytics(): Promise<UsageStats | null> {
  const res = await fetch('/api/admin/analytics')
  if (!res.ok) return null
  return res.json()
}

export async function shareJob(id: string): Promise<{ token: string; url: string }> {
  const res = await fetch(`/api/jobs/${id}/share`, { method: 'POST' })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `share failed (${res.status})`)
  }
  return res.json()
}
