import type { Job } from './jobs'
import { getJSON } from './http'

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
  q?: string
  sort?: string
  dir?: 'asc' | 'desc'
}

export async function listAllJobsPaged(params: ListJobsParams = {}): Promise<PagedJobs> {
  const qs = new URLSearchParams()
  if (params.limit != null) qs.set('limit', String(params.limit))
  if (params.offset != null) qs.set('offset', String(params.offset))
  if (params.status) qs.set('status', params.status)
  if (params.from) qs.set('from', params.from)
  if (params.to) qs.set('to', params.to)
  if (params.q) qs.set('q', params.q)
  if (params.sort) qs.set('sort', params.sort)
  if (params.dir) qs.set('dir', params.dir)
  return getJSON<PagedJobs>(`/api/admin/jobs?${qs.toString()}`)
}

export async function cancelJob(id: string): Promise<void> {
  const res = await fetch(`/api/admin/jobs/${id}/cancel`, { method: 'POST' })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `cancel failed (${res.status})`)
  }
}

export interface CancelAllResponse {
  cancelled: string[]
  already_terminal: string[]
  reaped_pids: number[]
  errors: string[]
  stop_all: boolean
}

export async function cancelAllJobs(): Promise<CancelAllResponse> {
  const res = await fetch('/api/admin/jobs/cancel-all', { method: 'POST' })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `cancel-all failed (${res.status})`)
  }
  return res.json()
}

export async function resumeAllJobs(): Promise<{ status: string; stop_all: boolean }> {
  const res = await fetch('/api/admin/jobs/resume-all', { method: 'POST' })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `resume-all failed (${res.status})`)
  }
  return res.json()
}

export interface SystemState {
  halted: boolean
  stop_all: boolean
}

export async function getSystemState(): Promise<SystemState> {
  return getJSON<SystemState>('/api/admin/system/state')
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
  admin_granted_by?: string
  admin_granted_at?: string
  created_at: string
  updated_at: string
}

export async function listUsers(): Promise<AdminUser[]> {
  return getJSON<AdminUser[]>('/api/admin/users')
}

export async function setUserRole(email: string, role: string): Promise<void> {
  const res = await fetch(`/api/admin/users/${encodeURIComponent(email)}/role`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role }),
  })
  if (!res.ok) {
    const text = await res.text()
    try {
      const data = JSON.parse(text)
      if (data && data.error) {
        throw new Error(data.error)
      }
    } catch (e) {
      if (e instanceof Error && !e.message.includes('JSON')) {
        throw e
      }
    }
    throw new Error(text || `role update failed (${res.status})`)
  }
}

export interface CatalogStatus {
  age_days: number
  stale: boolean
}

export async function getCatalogStatus(): Promise<CatalogStatus> {
  return getJSON<CatalogStatus>('/api/admin/catalog/status')
}

export async function triggerCatalogRefresh(): Promise<void> {
  const res = await fetch('/api/admin/catalog/refresh', { method: 'POST' })
  if (!res.ok) {
    const text = await res.text()
    try {
      const data = JSON.parse(text)
      throw new Error(data.reason || `refresh failed (${res.status})`)
    } catch {
      throw new Error(text || `refresh failed (${res.status})`)
    }
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

export interface ListAuditLogParams {
  limit?: number
  offset?: number
  actor?: string
  action?: string
  from?: string
  to?: string
}

export async function listAuditLog(params: ListAuditLogParams = {}): Promise<AuditEntry[]> {
  const qs = new URLSearchParams()
  if (params.limit != null) qs.set('limit', String(params.limit))
  if (params.offset != null) qs.set('offset', String(params.offset))
  if (params.actor) qs.set('actor', params.actor)
  if (params.action) qs.set('action', params.action)
  if (params.from) qs.set('from', params.from)
  if (params.to) qs.set('to', params.to)
  return getJSON<AuditEntry[]>(`/api/admin/audit?${qs.toString()}`)
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

export async function getAnalytics(): Promise<UsageStats> {
  return getJSON<UsageStats>('/api/admin/analytics')
}

export async function shareJob(id: string): Promise<{ token: string; url: string }> {
  const res = await fetch(`/api/jobs/${id}/share`, { method: 'POST' })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `share failed (${res.status})`)
  }
  return res.json()
}

export interface ReportShare {
  token: string // masked in list: first 6 chars + …
  job_id: string
  created_by: string
  created_at: string
  expires_at?: string
  expired: boolean
}

export async function listShares(): Promise<ReportShare[]> {
  return getJSON<ReportShare[]>('/api/admin/shares')
}

export async function revokeShare(token: string): Promise<void> {
  const res = await fetch(`/api/admin/shares/${encodeURIComponent(token)}`, {
    method: 'DELETE',
  })
  if (!res.ok && res.status !== 204) {
    const text = await res.text()
    throw new Error(text || `revoke failed (${res.status})`)
  }
}

export interface DeletedJob {
  id: string
  owner: string
  prospect: string
  status: string
  input_ext?: string
  aws_spend: number | null
  gcp_od?: number | null
  error: string
  session_id: string
  created_at: string
  deleted_at: string
  deleted_by: string
}

export async function listDeletedJobs(q?: string): Promise<DeletedJob[]> {
  const qs = new URLSearchParams()
  if (q) qs.set('q', q)
  const queryStr = qs.toString() ? `?${qs.toString()}` : ''
  return getJSON<DeletedJob[]>(`/api/admin/deleted-jobs${queryStr}`)
}

export interface Assumption {
  key: string
  value: number
  default_value: number
  min_value: number
  max_value: number
  unit: string
  title: string
  rationale: string
  category: string
  scope: string
  updated_by: string
  updated_at: string
}

export interface AssumptionHistory {
  id: number
  key: string
  old_value: number
  new_value: number
  reason: string
  changed_by: string
  changed_at: string
}

export interface AssumptionsResponse {
  assumptions: Assumption[]
  categories?: string[]
  version: string
}

export async function getAssumptions(): Promise<AssumptionsResponse> {
  return getJSON<AssumptionsResponse>('/api/admin/assumptions')
}

export interface PutAssumptionBody {
  value: number
  reason: string
  category?: string
}

export async function putAssumption(key: string, body: PutAssumptionBody): Promise<{ key: string; value: number; version: string }> {
  const res = await fetch(`/api/admin/assumptions/${encodeURIComponent(key)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `role update failed (${res.status})`)
  }
  return res.json()
}

export async function getAssumptionHistory(): Promise<AssumptionHistory[]> {
  return getJSON<AssumptionHistory[]>('/api/admin/assumptions/history')
}

// ─── Report settings ───

export interface ReportSetting {
  key: string
  value: string
  value_type: string
  default_value: string
  min_value: number | null
  max_value: number | null
  title: string
  description: string
  group_name: string
  sort_order: number
  updated_by: string
  updated_at: string
}

export interface ReportSettingChange {
  id: number
  key: string
  old_value: string
  new_value: string
  changed_by: string
  changed_at: string
}

export interface ReportSettingsResponse {
  settings: ReportSetting[]
  version: string
}

export async function getReportSettings(): Promise<ReportSettingsResponse> {
  return getJSON<ReportSettingsResponse>('/api/admin/report-settings')
}

export interface PutReportSettingBody {
  value: string
  reason: string
}

export async function putReportSetting(key: string, body: PutReportSettingBody): Promise<{ key: string; value: string; version: string }> {
  const res = await fetch(`/api/admin/report-settings/${encodeURIComponent(key)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `role update failed (${res.status})`)
  }
  return res.json()
}

export async function getReportSettingHistory(): Promise<ReportSettingChange[]> {
  return getJSON<ReportSettingChange[]>('/api/admin/report-settings/history')
}

export async function resetReportSettings(): Promise<{ version: string }> {
  const res = await fetch('/api/admin/report-settings/reset', { method: 'POST' })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `reset failed (${res.status})`)
  }
  return res.json()
}

export interface RegenerateReportResponse {
  id: string
  run_id: string
  status: string
  version: string
}

export async function regenerateReport(id: string): Promise<RegenerateReportResponse> {
  const res = await fetch(`/api/admin/jobs/${encodeURIComponent(id)}/regenerate-report`, {
    method: 'POST',
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `regenerate failed (${res.status})`)
  }
  return res.json()
}

export async function recalculateJob(id: string): Promise<RegenerateReportResponse> {
  const res = await fetch(`/api/admin/jobs/${encodeURIComponent(id)}/recalculate`, {
    method: 'POST',
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `recalculate failed (${res.status})`)
  }
  return res.json()
}

// ─── Job health diagnostics ───

export interface JobDiagnosticByCode {
  level: string
  count: number
  rows: number
  usd: number
  sources: string[]
}

export interface JobDiagnosticsSummary {
  workload_usd: number
  error_usd: number
  error_pct_of_workload: number
  error_count: number
  warn_count: number
  checks_not_evaluated: number
  by_code: Record<string, JobDiagnosticByCode>
}

export interface JobDiagnosticsResponse {
  available: boolean
  summary?: JobDiagnosticsSummary
  version?: string
}

export interface JobDiagnosticRecord {
  ts: string
  level: string
  code: string
  source: string
  phase: string | null
  message: string
  rows: number
  usd: number
  detail: Record<string, any>
}

export interface JobDiagnosticRecordsResponse {
  available: boolean
  records: JobDiagnosticRecord[]
}

export async function getJobDiagnostics(id: string): Promise<JobDiagnosticsResponse> {
  return getJSON<JobDiagnosticsResponse>(`/api/admin/jobs/${encodeURIComponent(id)}/diagnostics`)
}

export async function getJobDiagnosticRecords(id: string): Promise<JobDiagnosticRecordsResponse> {
  return getJSON<JobDiagnosticRecordsResponse>(`/api/admin/jobs/${encodeURIComponent(id)}/diagnostics/records`)
}

// ─── Notification health ───

export interface NotifyChannelStatus {
  configured: boolean
}

export interface NotifyStatus {
  email: NotifyChannelStatus
  slack: NotifyChannelStatus
}

export async function getNotifyStatus(): Promise<NotifyStatus> {
  return getJSON<NotifyStatus>('/api/admin/notify/status')
}

export async function notifyTest(): Promise<{ status: string }> {
  const res = await fetch('/api/admin/notify/test', { method: 'POST' })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `notify test failed (${res.status})`)
  }
  return res.json()
}

// ─── Ticket management ───

export interface UploadedBill {
  content_hash: string
  input_ext: string
  file_size: number
  file_name: string
  created_at: string
}

export interface BillPreviewResponse {
  file_name: string
  content_hash: string
  input_ext: string
  file_size: number
  lines: string[]
  total_lines_read: number
  is_truncated: boolean
}

export interface UploadedBillsResponse {
  bills: UploadedBill[]
}

export async function listUploadedBills(): Promise<UploadedBillsResponse> {
  return getJSON<UploadedBillsResponse>('/api/admin/bills')
}

export async function previewUploadedBill(hash: string): Promise<BillPreviewResponse> {
  return getJSON<BillPreviewResponse>(`/api/admin/bills/${encodeURIComponent(hash)}/preview`)
}

export interface Ticket {
  id: string
  title: string
  description: string
  status: string
  priority: string
  category: string
  created_by: string
  created_at: string
  updated_at: string
  resolved_at?: string
  resolved_by?: string
}

export interface TicketListResponse {
  tickets: Ticket[]
}

export interface CreateTicketBody {
  title: string
  description: string
  priority?: string
  category?: string
}

export interface UpdateTicketBody {
  status?: string
  priority?: string
  category?: string
  title?: string
  description?: string
}

export async function createTicket(body: CreateTicketBody): Promise<Ticket> {
  const res = await fetch('/api/admin/tickets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `create ticket failed (${res.status})`)
  }
  return res.json()
}

export async function listTickets(): Promise<TicketListResponse> {
  return getJSON<TicketListResponse>('/api/admin/tickets')
}

export async function getTicket(id: string): Promise<Ticket> {
  return getJSON<Ticket>(`/api/admin/tickets/${encodeURIComponent(id)}`)
}

export async function updateTicket(id: string, body: UpdateTicketBody): Promise<Ticket> {
  const res = await fetch(`/api/admin/tickets/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `update ticket failed (${res.status})`)
  }
  return res.json()
}

export interface AdminJobNotif {
  id: string
  prospect: string
  owner: string
  status: string
  error: string
  aws_spend: number | null
  gcp_od: number | null
  created_at: string
}

export interface NotificationItem {
  id: string
  type: 'job_failed' | 'job_completed' | 'ticket_raised' | 'assumption_changed' | 'setting_changed' | string
  title: string
  message: string
  severity: 'critical' | 'warning' | 'info' | 'success' | string
  actor: string
  link: string
  timestamp: string
}

export interface AdminNotificationsResponse {
  admin_email: string
  feed: NotificationItem[]
  my_jobs: AdminJobNotif[]
  failed_jobs: AdminJobNotif[]
  tickets: Ticket[]
  assumption_changes?: any[]
  setting_changes?: any[]
}

export async function getAdminNotifications(): Promise<AdminNotificationsResponse> {
  return getJSON<AdminNotificationsResponse>('/api/admin/notifications')
}
// ── Notification integrations (email + Slack) ──────────────────────────────
export interface SecretState { configured: boolean; last4?: string }
export interface IntegrationsConfig {
  encryption_available: boolean
  email: { enabled: boolean; from: string; api_key: SecretState; env_fallback: boolean }
  slack: { enabled: boolean; webhook_url: SecretState; env_fallback: boolean }
}
export interface IntegrationsUpdate {
  email?: { enabled?: boolean; from?: string; api_key?: string }
  slack?: { enabled?: boolean; webhook_url?: string }
}

export async function getIntegrations(): Promise<IntegrationsConfig> {
  return getJSON<IntegrationsConfig>('/api/admin/integrations')
}

export async function updateIntegrations(body: IntegrationsUpdate): Promise<IntegrationsConfig> {
  const res = await fetch('/api/admin/integrations', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error((await res.text()) || `save failed (${res.status})`)
  return res.json() as Promise<IntegrationsConfig>
}

export async function testNotify(channel: 'email' | 'slack'): Promise<{ status: string; error?: string }> {
  const res = await fetch('/api/admin/notify/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ channel }),
  })
  return res.json() as Promise<{ status: string; error?: string }>
}
