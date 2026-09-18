import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Summary } from '../components/Summary'
import { getSharedReport } from '../api/reports'
import type { SharedReport as SharedReportData } from '../api/reports'
import { useTitle } from '../lib/useTitle'
import facetsFIcon from '../assets/facets-logo-f.svg'

export function SharedReport() {
  const { token } = useParams<{ token: string }>()
  const [report, setReport] = useState<SharedReportData | null>(null)
  const [error, setError] = useState<string | null>(null)

  useTitle(report ? `${report.prospect} — shared report` : 'Shared report')

  useEffect(() => {
    if (!token) return
    getSharedReport(token)
      .then(setReport)
      .catch(err => setError(err instanceof Error ? err.message : 'not found'))
  }, [token])

  return (
    <div className="min-h-screen bg-[#0a0a0f] text-white">
      <nav className="flex items-center px-6 py-3 border-b border-white/10 bg-[#0a0a0f]/80 sticky top-0 z-30">
        <img src={facetsFIcon} alt="Facets" className="h-5" />
        <span className="ml-3 text-sm font-semibold tracking-tight text-white/70">Shared report (read-only)</span>
      </nav>
      <div className="max-w-3xl mx-auto px-6 py-10">
        {error && (
          <div className="bg-orange-400/10 border border-orange-400/30 rounded-lg p-4 text-sm text-orange-400">
            This link is invalid or has expired: {error}
          </div>
        )}
        {!error && !report && (
          <p className="text-sm text-gray-500 py-12 text-center">Loading…</p>
        )}
        {report && (
          <div className="space-y-6">
            <h1 className="text-2xl font-semibold">
              Report for <span className="text-[#645DF6]">{report.prospect}</span>
            </h1>
            <p className="text-xs text-gray-500">
              Status: {report.status} · Created {new Date(report.created_at).toLocaleString()}
            </p>
            {report.summary_md ? (
              <Summary markdown={report.summary_md} />
            ) : (
              <p className="text-sm text-gray-500">No summary available for this report yet.</p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
