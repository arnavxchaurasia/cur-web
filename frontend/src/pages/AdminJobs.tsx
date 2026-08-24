import { useEffect, useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { Nav } from '../components/Nav'
import { Reveal } from '../components/Reveal'
import { listAllJobs } from '../api/jobs'
import type { Job } from '../api/jobs'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'

const statusColor: Record<string, string> = {
  pending: 'text-gray-400',
  running: 'text-[#00C2BB]',
  done:    'text-[#00C2BB]',
  failed:  'text-orange-400',
}

const statusIcon: Record<string, string> = {
  done: '✓', failed: '✕', pending: '–', running: '●',
}

function fmtMoney(v: number | null) {
  if (v == null) return '—'
  return v.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
}

function fmtWhen(iso: string) {
  const d = new Date(iso)
  return d.toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short' })
}

type StatCardProps = Readonly<{ label: string; value: number; color?: string; delay?: number }>
function StatCard({ label, value, color = 'text-white', delay = 0 }: StatCardProps) {
  return (
    <div
      className="bg-white/[0.03] border border-white/10 rounded-lg px-5 py-4 card-lift anim-fade-in-up"
      style={{ animationDelay: `${delay}ms` }}
    >
      <p className="text-xs uppercase tracking-wider text-gray-400 mb-1">{label}</p>
      <p className={`text-2xl font-semibold tabular-nums ${color}`}>{value}</p>
    </div>
  )
}

export function AdminJobs({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('All reports — admin')
  const nav = useNavigate()
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [query, setQuery] = useState<string>('')

  useEffect(() => {
    listAllJobs().then(setJobs)
    const ticker = setInterval(() => listAllJobs().then(setJobs), 30_000)
    return () => clearInterval(ticker)
  }, [])

  const filtered = useMemo(() => {
    if (!jobs) return []
    const q = query.trim().toLowerCase()
    if (!q) return jobs
    return jobs.filter(j =>
      j.owner.toLowerCase().includes(q) ||
      j.prospect.toLowerCase().includes(q) ||
      j.status.toLowerCase().includes(q)
    )
  }, [jobs, query])

  const totals = useMemo(() => {
    if (!jobs) return { total: 0, done: 0, failed: 0, running: 0 }
    return {
      total:   jobs.length,
      done:    jobs.filter(j => j.status === 'done').length,
      failed:  jobs.filter(j => j.status === 'failed').length,
      running: jobs.filter(j => j.status === 'running' || j.status === 'pending').length,
    }
  }, [jobs])

  return (
    <div className="min-h-screen text-white">
      <Nav user={user} />
      <div className="max-w-6xl mx-auto px-6 py-10">

        <div className="mb-8 anim-fade-in-up">
          <h1 className="text-2xl font-semibold">All reports</h1>
          <p className="text-sm text-gray-500 mt-1">
            Every projection across all FSRs. Click a row to open the report.
          </p>
        </div>

        {jobs && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-8">
            <StatCard label="Total"  value={totals.total}   color="text-white"        delay={50}  />
            <StatCard label="Done"   value={totals.done}    color="text-[#00C2BB]"    delay={110} />
            <StatCard label="Active" value={totals.running} color="text-[#645DF6]"    delay={170} />
            <StatCard label="Failed" value={totals.failed}  color="text-orange-400"   delay={230} />
          </div>
        )}

        {jobs && jobs.length > 0 && (
          <div className="mb-5 anim-fade-in-up delay-250">
            <input
              type="text"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Filter by owner, prospect, or status…"
              className="w-full sm:w-80 bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm
                placeholder:text-gray-500 transition-colors duration-150 focus:border-[#645DF6]"
            />
            {query && (
              <span className="ml-3 text-xs text-gray-500">
                {filtered.length} of {jobs.length}
              </span>
            )}
          </div>
        )}

        {jobs === null && (
          <div className="flex items-center gap-2 text-sm text-gray-500 py-12 justify-center">
            <span className="relative flex w-2 h-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#645DF6] opacity-60" />
              <span className="relative inline-flex rounded-full w-2 h-2 bg-[#645DF6]" />
            </span>{' '}
            Loading…
          </div>
        )}
        {jobs !== null && filtered.length === 0 && (
          <p className="text-sm text-gray-500 py-12 text-center">No reports.</p>
        )}

        {filtered.length > 0 && (
          <Reveal>
            <div className="border border-white/10 rounded-xl overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-white/[0.03] text-xs uppercase tracking-wider text-gray-400">
                  <tr>
                    <th className="text-left px-4 py-3">When</th>
                    <th className="text-left px-4 py-3">Owner</th>
                    <th className="text-left px-4 py-3">Prospect</th>
                    <th className="text-left px-4 py-3">Status</th>
                    <th className="text-right px-4 py-3">AWS spend (pre-tax)</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/5">
                  {filtered.map(j => (
                    <tr key={j.id} onClick={() => nav(`/jobs/${j.id}`)}
                        className="cursor-pointer hover:bg-white/[0.04] transition-colors duration-100">
                      <td className="px-4 py-3 text-gray-400 whitespace-nowrap">{fmtWhen(j.created_at)}</td>
                      <td className="px-4 py-3 text-gray-300">{j.owner}</td>
                      <td className="px-4 py-3 text-gray-200 font-medium">{j.prospect}</td>
                      <td className={`px-4 py-3 ${statusColor[j.status] ?? 'text-gray-400'}`}>
                        <span className="flex items-center gap-1.5">
                          <span className={`text-xs font-bold ${j.status === 'running' ? 'animate-pulse' : ''}`}>
                            {statusIcon[j.status] ?? ''}
                          </span>
                          {j.status.charAt(0).toUpperCase() + j.status.slice(1)}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right text-gray-300 tabular-nums">{fmtMoney(j.aws_spend)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Reveal>
        )}
      </div>
    </div>
  )
}
