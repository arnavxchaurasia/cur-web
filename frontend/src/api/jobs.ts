export interface Job {
  id: string; owner: string; prospect: string; status: string
  aws_spend: number | null; gcp_od?: number | null; error: string; created_at: string
  engine?: string; engine_version?: string; assumptions_version?: string
}

export interface UserPreferences {
  default_engine: string
  default_export_format: string
  email_notifications: boolean
}

export async function getUserPreferences(): Promise<UserPreferences> {
  const res = await fetch('/api/user/preferences')
  if (!res.ok) {
    return { default_engine: 'gemini-3.8-flash', default_export_format: 'html', email_notifications: true }
  }
  return res.json()
}

export async function updateUserPreferences(prefs: Partial<UserPreferences>): Promise<UserPreferences> {
  const res = await fetch('/api/user/preferences', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(prefs),
  })
  if (!res.ok) throw new Error('Failed to update preferences')
  return res.json()
}

export interface EngineSettings {
  default_engine: string
  allowed_engines: string[]
}

export async function getEngineSettings(): Promise<EngineSettings> {
  const res = await fetch('/api/engine-settings')
  if (!res.ok) {
    return { default_engine: 'gemini-3.8-flash', allowed_engines: ['gemini-3.8-flash'] }
  }
  return res.json()
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

export interface BillCurrency {
  currency: string
  symbol: string
  fx_rate: number
  aws_fx_rate: number
  aws_cost_currency: string
  label: string
  source: string
}

export interface RunsResponse {
  runs: RunResult[]
  currency: BillCurrency
}

async function computeFileSHA256(file: File): Promise<string> {
  const buffer = await file.arrayBuffer()
  const hashBuffer = await crypto.subtle.digest('SHA-256', buffer)
  const hashArray = Array.from(new Uint8Array(hashBuffer))
  return hashArray.map(b => b.toString(16).padStart(2, '0')).join('')
}

export async function createJob(
  file: File,
  prospect: string,
  onProgress?: (percent: number, statusText?: string) => void,
  engine?: string
): Promise<{ id: string }> {
  // 1. Check if identical bill is already saved on server (deduplication)
  try {
    onProgress?.(0, 'Checking for existing bill…')
    const hash = await computeFileSHA256(file)
    const checkRes = await fetch(`/api/bills/check?hash=${hash}`)
    if (checkRes.ok) {
      const info = await checkRes.json()
      if (info?.exists) {
        onProgress?.(100, 'Identical bill found! Starting pipeline…')
        const payload: Record<string, string> = { content_hash: hash, prospect_name: prospect }
        if (engine) payload.engine = engine
        const reuseRes = await fetch('/api/jobs/existing-bill', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        })
        if (reuseRes.ok) {
          return reuseRes.json()
        }
      }
    }
  } catch {
    // If hash check fails or is unsupported, fall through to regular upload
  }

  const CHUNK_SIZE = 30 * 1024 * 1024 // 30 MB chunks to safely stay within 100MB proxy limits

  // If file is smaller than or equal to 80 MB, upload directly in one piece for high speed
  if (file.size <= 80 * 1024 * 1024) {
    onProgress?.(10, 'Uploading bill…')
    const form = new FormData()
    form.append('file', file)
    form.append('prospect_name', prospect)
    if (engine) form.append('engine', engine)

    let res: Response
    try {
      res = await fetch('/api/jobs', { method: 'POST', body: form })
    } catch {
      throw new Error('Network error: Failed to reach the server. Please check your internet connection.')
    }

    if (!res.ok) {
      const errText = await res.text().catch(() => '')
      try {
        const parsed = JSON.parse(errText)
        throw new Error(parsed?.error || `Upload failed (HTTP ${res.status})`)
      } catch (e: any) {
        throw new Error(errText || e?.message || `Upload failed (HTTP ${res.status})`)
      }
    }
    onProgress?.(100)
    return res.json()
  }

  // Large file (> 80 MB): Slice into 30 MB chunks, upload sequentially, and combine on server
  const uploadId = crypto.randomUUID()
  const totalChunks = Math.ceil(file.size / CHUNK_SIZE)

  for (let i = 0; i < totalChunks; i++) {
    const start = i * CHUNK_SIZE
    const end = Math.min(start + CHUNK_SIZE, file.size)
    const chunkBlob = file.slice(start, end)

    const chunkForm = new FormData()
    chunkForm.append('upload_id', uploadId)
    chunkForm.append('chunk_index', String(i))
    chunkForm.append('total_chunks', String(totalChunks))
    chunkForm.append('chunk', chunkBlob, file.name)

    let chunkRes: Response
    try {
      chunkRes = await fetch('/api/jobs/chunk', { method: 'POST', body: chunkForm })
    } catch {
      throw new Error(`Connection lost while uploading part ${i + 1} of ${totalChunks}. Please try again.`)
    }

    if (!chunkRes.ok) {
      const msg = await chunkRes.text().catch(() => '')
      throw new Error(`Upload of part ${i + 1} failed: ${msg || chunkRes.statusText}`)
    }

    const percent = Math.round(((i + 1) / totalChunks) * 95)
    onProgress?.(percent)
  }

  // All chunks uploaded: tell backend to reassemble and start the pipeline
  let completeRes: Response
  try {
    const completePayload: Record<string, any> = {
      upload_id: uploadId,
      prospect_name: prospect,
      file_name: file.name,
      total_chunks: totalChunks,
    }
    if (engine) completePayload.engine = engine
    completeRes = await fetch('/api/jobs/complete-chunked', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(completePayload),
    })
  } catch {
    throw new Error('Connection lost while assembling bill on server. Please try again.')
  }

  if (!completeRes.ok) {
    const msg = await completeRes.text().catch(() => '')
    try {
      const parsed = JSON.parse(msg)
      throw new Error(parsed?.error || `Assembly failed (HTTP ${completeRes.status})`)
    } catch (e: any) {
      throw new Error(msg || e?.message || `Assembly failed (HTTP ${completeRes.status})`)
    }
  }

  onProgress?.(100)
  return completeRes.json()
}

export async function listJobs(): Promise<Job[]> {
  const res = await fetch('/api/jobs')
  if (!res.ok) return []
  return res.json()
}

export async function getJob(id: string): Promise<Job> {
  const res = await fetch(`/api/jobs/${id}`)
  if (!res.ok) throw new Error('not found')
  return res.json()
}

export function downloadURL(id: string, runId?: string, format?: string, level?: string): string {
  const params = new URLSearchParams()
  if (runId) params.set('run_id', runId)
  if (format) params.set('format', format)
  if (level) params.set('level', level)
  const qs = params.toString()
  return qs ? `/api/jobs/${id}/download?${qs}` : `/api/jobs/${id}/download`
}

export async function getRuns(id: string): Promise<RunsResponse> {
  const res = await fetch(`/api/jobs/${id}/runs`)
  if (!res.ok) return { runs: [], currency: { currency: 'USD', symbol: '$', fx_rate: 1.0, aws_fx_rate: 1.0, aws_cost_currency: 'USD', label: 'USD', source: 'default' } }
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
  phase_durations?: Record<string, { num?: number; name?: string; duration_seconds?: number; completed_at?: string }>
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

export async function cancelJob(id: string): Promise<{ id: string; status: string }> {
  const res = await fetch(`/api/jobs/${id}/cancel`, { method: 'POST' })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `cancel failed (${res.status})`)
  }
  return res.json()
}

export async function deleteJob(id: string): Promise<void> {
  const res = await fetch(`/api/jobs/${id}`, { method: 'DELETE' })
  if (!res.ok && res.status !== 204) {
    let msg = `delete failed (${res.status})`
    try {
      const text = await res.text()
      if (text) msg = text
    } catch {
      // ignore
    }
    throw new Error(msg)
  }
}

export async function regenerateReport(id: string): Promise<{ id: string; run_id: string; status: string; version: string }> {
  const res = await fetch(`/api/admin/jobs/${encodeURIComponent(id)}/regenerate-report`, {
    method: 'POST',
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `regenerate failed (${res.status})`)
  }
  return res.json()
}

export async function recalculateJob(id: string): Promise<{ id: string; run_id: string; status: string; version: string }> {
  const res = await fetch(`/api/admin/jobs/${encodeURIComponent(id)}/recalculate`, {
    method: 'POST',
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `recalculate failed (${res.status})`)
  }
  return res.json()
}

// ─── Run comparison ───

export interface RunComparison {
  run_a: RunResult
  run_b: RunResult
  aws_delta: number
  gcp_od_delta: number
  gcp_1yr_cud_delta: number
  gcp_3yr_cud_delta: number
  assumptions_version_a?: string
  assumptions_version_b?: string
  report_settings_version_a?: string
  report_settings_version_b?: string
  catalog_vintage_a?: string
  catalog_vintage_b?: string
}

export async function compareRuns(id: string, from: string, to: string): Promise<RunComparison | null> {
  const res = await fetch(`/api/jobs/${encodeURIComponent(id)}/compare?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`)
  if (!res.ok) return null
  return res.json()
}

