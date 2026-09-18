// frontend/src/api/reports.ts

export interface SharedReport {
  prospect: string
  status: string
  created_at: string
  summary_md?: string
}

export async function getSharedReport(token: string): Promise<SharedReport> {
  const res = await fetch(`/api/reports/${token}`)
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(text || `not found (${res.status})`)
  }
  return res.json()
}
