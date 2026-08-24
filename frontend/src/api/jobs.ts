// frontend/src/api/jobs.ts
export interface Job {
  id: string; owner: string; prospect: string; status: string
  aws_spend: number | null; error: string; created_at: string
}

export interface RunResult {
  run_id: string
  ts_utc: string
  run_type: 'initial' | 'refinement'
  instruction: string | null
  aws_total: number
  gcp_od: number
  gcp_1yr_cud: number
  gcp_3yr_cud: number
  report_html: string
  report_md: string
  summary_md: string | null
  mapped_rows: number
  passthroughs: number
  confidence: string | null
}

export async function createJob(file: File, prospect: string): Promise<{ id: string }> {
  const form = new FormData()
  form.append('file', file)
  form.append('prospect_name', prospect)
  const res = await fetch('/api/jobs', { method: 'POST', body: form })
  if (!res.ok) throw new Error('upload failed')
  return res.json()
}

export async function listJobs(): Promise<Job[]> {
  const res = await fetch('/api/jobs')
  if (!res.ok) return []
  return res.json()
}

export async function listAllJobs(): Promise<Job[]> {
  const res = await fetch('/api/admin/jobs')
  if (!res.ok) return []
  return res.json()
}

export async function getJob(id: string): Promise<Job> {
  const res = await fetch(`/api/jobs/${id}`)
  if (!res.ok) throw new Error('not found')
  return res.json()
}

export function downloadURL(id: string, runId?: string): string {
  return runId
    ? `/api/jobs/${id}/download?run_id=${encodeURIComponent(runId)}`
    : `/api/jobs/${id}/download`
}

export async function getRuns(id: string): Promise<RunResult[]> {
  const res = await fetch(`/api/jobs/${id}/runs`)
  if (!res.ok) return []
  return res.json()
}

export async function getSummary(id: string, runId?: string): Promise<string | null> {
  const url = runId
    ? `/api/jobs/${id}/summary?run_id=${encodeURIComponent(runId)}`
    : `/api/jobs/${id}/summary`
  const res = await fetch(url)
  if (res.status === 404) return null
  if (!res.ok) return null
  return res.text()
}

export interface Progress {
  events: number
  phase: string
  phase_number: number
  last_activity: string
  transcript_ok: boolean
}

export async function getProgress(id: string): Promise<Progress | null> {
  const res = await fetch(`/api/jobs/${id}/progress`)
  if (!res.ok) return null
  return res.json()
}

export async function retryJob(id: string): Promise<{ status: string }> {
  const res = await fetch(`/api/jobs/${id}/retry`, { method: 'POST' })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `retry failed (${res.status})`)
  }
  return res.json()
}

export async function refineJob(id: string, instruction: string): Promise<{ status: string }> {
  const res = await fetch(`/api/jobs/${id}/refine`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ instruction }),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `refine failed (${res.status})`)
  }
  return res.json()
}
