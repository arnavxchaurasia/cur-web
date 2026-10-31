import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Layout } from '../components/Layout'
import { listJobs, downloadURL, type Job } from '../api/jobs'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'
import { formatMoney } from '../lib/preferencesContext'
import { BackButton } from '../components/BackButton'

function statusBadge(status: string) {
  switch (status) {
    case 'done':
      return <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-[#10B981]/15 text-[#10B981] border border-[#10B981]/30">DONE</span>
    case 'running':
      return <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-[#00C2BB]/15 text-[#00C2BB] border border-[#00C2BB]/30 animate-pulse">RUNNING</span>
    case 'failed':
      return <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-red-500/15 text-red-400 border border-red-500/30">FAILED</span>
    default:
      return <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-white/10 text-gray-400 border border-white/20 uppercase">{status}</span>
  }
}

export function ReleaseDashboard({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Release Dashboard')
  const [jobs, setJobs] = useState<Job[]>([])
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')

  useEffect(() => {
    setLoading(true)
    listJobs()
      .then(setJobs)
      .finally(() => setLoading(false))
  }, [])

  const filtered = jobs.filter(j =>
    j.prospect.toLowerCase().includes(query.toLowerCase()) ||
    j.owner.toLowerCase().includes(query.toLowerCase()) ||
    j.id.toLowerCase().includes(query.toLowerCase())
  )

  return (
    <Layout user={user}>
      <div className="max-w-6xl mx-auto px-6 py-8 anim-fade-in-up">
        <BackButton fallback="/" />
        {/* Header */}
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-6">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Release Dashboard</h1>
            <p className="text-xs text-gray-400 mt-1">
              Active and completed cost projection releases across all your cloud customer pipelines.
            </p>
          </div>
          <div className="flex items-center gap-3">
            <Link
              to="/"
              className="btn-shimmer px-4 py-2 rounded-lg text-xs font-semibold text-white shadow-sm transition-transform hover:scale-102 cursor-pointer"
            >
              + New Estimation Release
            </Link>
          </div>
        </div>

        {/* Filter bar */}
        <div className="mb-6 flex items-center gap-3">
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search by prospect, owner or release ID…"
            className="w-full sm:w-80 bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-xs text-white placeholder-gray-500 outline-none focus:border-[#645DF6] transition-colors"
          />
          <span className="text-xs text-gray-500">
            {filtered.length} release{filtered.length === 1 ? '' : 's'}
          </span>
        </div>

        {/* Releases Table */}
        <div className="border border-white/10 rounded-xl overflow-hidden bg-white/[0.02]">
          {loading ? (
            <div className="py-16 text-center text-gray-500 text-xs">
              <div className="flex items-center justify-center gap-2 mb-2">
                <div className="w-2 h-2 rounded-full bg-[#00C2BB] animate-ping" />
              </div>
              Loading releases…
            </div>
          ) : filtered.length === 0 ? (
            <div className="py-16 text-center text-gray-500 text-xs">
              No releases found. Start a new release from <Link to="/" className="text-[#00C2BB] hover:underline">Projects</Link>.
            </div>
          ) : (
            <table className="w-full text-left text-xs">
              <thead className="bg-white/[0.03] text-[11px] uppercase tracking-wider text-gray-400 border-b border-white/10">
                <tr>
                  <th className="px-5 py-3">Release / Prospect</th>
                  <th className="px-5 py-3">Status</th>
                  <th className="px-5 py-3 text-right">AWS Spend</th>
                  <th className="px-5 py-3 text-right">GCP OD Est.</th>
                  <th className="px-5 py-3">Created</th>
                  <th className="px-5 py-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {filtered.map(j => (
                  <tr key={j.id} className="hover:bg-white/[0.02] transition-colors group">
                    <td className="px-5 py-3.5">
                      <Link to={`/jobs/${j.id}`} className="font-semibold text-white hover:text-[#00C2BB] transition-colors block">
                        {j.prospect}
                      </Link>
                      <span className="font-mono text-[10px] text-gray-500">{j.id}</span>
                    </td>
                    <td className="px-5 py-3.5">
                      {statusBadge(j.status)}
                    </td>
                    <td className="px-5 py-3.5 text-right font-mono text-gray-300">
                      {j.aws_spend != null ? formatMoney(j.aws_spend, "$") : '—'}
                    </td>
                    <td className="px-5 py-3.5 text-right font-mono text-[#00C2BB]">
                      {j.gcp_od != null ? formatMoney(j.gcp_od, "$") : '—'}
                    </td>
                    <td className="px-5 py-3.5 text-gray-400 whitespace-nowrap">
                      {new Date(j.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                    </td>
                    <td className="px-5 py-3.5 text-right whitespace-nowrap">
                      <div className="flex items-center justify-end gap-2">
                        <Link
                          to={`/jobs/${j.id}`}
                          className="px-2.5 py-1 rounded bg-white/5 hover:bg-white/10 text-white font-medium transition-colors"
                        >
                          View Release
                        </Link>
                        {j.status === 'done' && (
                          <a
                            href={downloadURL(j.id)}
                            className="px-2 py-1 rounded border border-white/10 hover:border-white/30 text-gray-300 hover:text-white transition-colors"
                            title="Download HTML"
                          >
                            HTML
                          </a>
                        )}
                        <a
                          href={downloadURL(j.id, undefined, 'bill')}
                          className="px-2 py-1 rounded border border-[#00C2BB]/30 text-[#00C2BB] hover:bg-[#00C2BB]/10 transition-colors"
                          title="Download Original Bill"
                        >
                          Bill
                        </a>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </Layout>
  )
}
export default ReleaseDashboard
