// frontend/src/api/facets.ts
import type { Job } from './jobs'

export interface PortfolioCategoryRow {
  category: string
  line_items: number
  aws_total: number
  gcp_od: number
  diff: number
  diff_pct: number
  share_of_aws_spend?: number
}

export interface PortfolioCustomerRow {
  job_dir: string
  customer: string
  line_items: number
  aws_total: number
  aws_mapped_cost: number
  gcp_od: number
  diff: number
  diff_pct: number
  categories: PortfolioCategoryRow[]
}

export interface PortfolioSummary {
  totals: {
    customers: number
    line_items: number
    aws_total: number
    aws_mapped_cost: number
    gcp_od: number
    diff: number
    diff_pct: number
  }
  by_customer: PortfolioCustomerRow[]
  by_category: PortfolioCategoryRow[]
}

export async function facetsListJobs(): Promise<Job[]> {
  const res = await fetch('/api/facets/jobs')
  if (!res.ok) return []
  return res.json()
}

export async function facetsGetLogs(jobId: string): Promise<string[]> {
  const res = await fetch(`/api/facets/jobs/${jobId}/logs`)
  if (!res.ok) return []
  const data = await res.json()
  return data.lines ?? []
}

export function facetsPortfolioSummaryHTMLURL(jobIds: string[]): string {
  return `/api/facets/portfolio-summary.html?job_ids=${encodeURIComponent(jobIds.join(','))}`
}

export function facetsPortfolioSummaryXLSXURL(jobIds: string[]): string {
  return `/api/facets/portfolio-summary.xlsx?job_ids=${encodeURIComponent(jobIds.join(','))}`
}

export async function facetsGeneratePortfolioSummary(jobIds: string[]): Promise<PortfolioSummary> {
  const res = await fetch('/api/facets/portfolio-summary', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job_ids: jobIds }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: 'summary generation failed' }))
    throw new Error(err.error || 'summary generation failed')
  }
  return res.json()
}
