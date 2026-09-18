import { useEffect, useState, useMemo } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { Nav } from '../components/Nav'
import { Reveal } from '../components/Reveal'
import { retryJob } from '../api/jobs'
import type { Job } from '../api/jobs'
import { listAllJobsPaged, cancelJob, deleteJob } from '../api/admin'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'

const statusColor: Record<string, string> = {
  pending: 'text-gray-400',
  running: 'text-[#00C2BB]',
  done:    'text-[#00C2BB]',
  failed:  'text-orange-400',
  cancelled: 'text-gray-500',
}

const statusIcon: Record<string, string> = {
  done: '✓', failed: '✕', pending: '–', running: '●', cancelled: '⊘',
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
export function StatCard({ label, value, color = 'text-white', delay = 0 }: StatCardProps) {
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

const PAGE_SIZE = 25

export function AdminJobs({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('All reports — admin')
  const nav = useNavigate()
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [statusFilter, setStatusFilter] = useState('')
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')
  const [query, setQuery] = useState<string>('')
  const [busy, setBusy] = useState<Set<string>>(new Set())
  const [rowError, setRowError] = useState<Record<string, string>>({})
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())

  const toggleGroup = (owner: string) => setCollapsed(prev => {
    const next = new Set(prev)
    next.has(owner) ? next.delete(owner) : next.add(owner)
    return next
  })

  const refresh = () => listAllJobsPaged({
    limit: PAGE_SIZE,
    offset,
    status: statusFilter || undefined,
    from: fromDate || undefined,
    to: toDate || undefined,
  }).then(res => { setJobs(res.jobs); setTotal(res.total) })

  const handleRetry = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation()
    setBusy(prev => new Set(prev).add(id))
    setRowError(prev => { const next = { ...prev }; delete next[id]; return next })
    try {
      await retryJob(id)
      await refresh()
    } catch (err) {
      setRowError(prev => ({ ...prev, [id]: err instanceof Error ? err.message : 'retry failed' }))
    } finally {
      setBusy(prev => { const next = new Set(prev); next.delete(id); return next })
    }
  }

  const handleCancel = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation()
    if (!confirm('Cancel this job?')) return
    setBusy(prev => new Set(prev).add(id))
    try {
      await cancelJob(id)
      await refresh()
    } catch (err) {
      setRowError(prev => ({ ...prev, [id]: err instanceof Error ? err.message : 'cancel failed' }))
    } finally {
      setBusy(prev => { const next = new Set(prev); next.delete(id); return next })
    }
  }

  const handleDelete = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation()
    if (!confirm('Permanently delete this job and its files?')) return
    setBusy(prev => new Set(prev).add(id))
    try {
      await deleteJob(id)
      await refresh()
    } catch (err) {
      setRowError(prev => ({ ...prev, [id]: err instanceof Error ? err.message : 'delete failed' }))
    } finally {
      setBusy(prev => { const next = new Set(prev); next.delete(id); return next })
    }
  }

  useEffect(() => {
    refresh()
    const ticker = setInterval(refresh, 30_000)
    return () => clearInterval(ticker)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offset, statusFilter, fromDate, toDate])

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

  const groups = useMemo(() => {
    const byOwner = new Map<string, Job[]>()
    for (const j of filtered) {
      const list = byOwner.get(j.owner)
      if (list) list.push(j)
      else byOwner.set(j.owner, [j])
    }
    return Array.from(byOwner.entries())
      .map(([owner, list]) => ({
        owner,
        jobs: list,
        done: list.filter(j => j.status === 'done').length,
        failed: list.filter(j => j.status === 'failed').length,
        latest: list[0]?.created_at ?? '',
      }))
      .sort((a, b) => b.latest.localeCompare(a.latest))
  }, [filtered])

  const page = Math.floor(offset / PAGE_SIZE) + 1
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="min-h-screen text-white">
      <Nav user={user} />
      <div className="max-w-6xl mx-auto px-6 py-10">

        <div className="mb-8 anim-fade-in-up flex items-start justify-between gap-4 flex-wrap">
          <div>
            <h1 className="text-2xl font-semibold">All reports</h1>
            <p className="text-sm text-gray-500 mt-1">
              Every projection across all FSRs. Click a row to open the report.
            </p>
          </div>
          <div className="flex gap-3 text-xs">
            <Link to="/admin/users" className="nav-link text-[#645DF6]">Users</Link>
            <Link to="/admin/catalog" className="nav-link text-[#645DF6]">Catalog</Link>
            <Link to="/admin/analytics" className="nav-link text-[#645DF6]">Analytics</Link>
            <Link to="/admin/audit" className="nav-link text-[#645DF6]">Audit log</Link>
          </div>
        </div>

        {jobs && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-8">
            <StatCard label="Total"  value={totals.total}   color="text-white"        delay={50}  />
            <StatCard label="Done"   value={totals.done}    color="text-[#00C2BB]"    delay={110} />
            <StatCard label="Active" value={totals.running} color="text-[#645DF6]"    delay={170} />
            <StatCard label="Failed" value={totals.failed}  color="text-orange-400"   delay={230} />
          </div>
        )}

        <div className="mb-5 anim-fade-in-up delay-250 flex flex-wrap items-center gap-3">
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Filter by owner, prospect, or status…"
            className="w-full sm:w-72 bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm
              placeholder:text-gray-500 transition-colors duration-150 focus:border-[#645DF6]"
          />
          <select
            value={statusFilter}
            onChange={e => { setStatusFilter(e.target.value); setOffset(0) }}
            className="bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm"
          >
            <option value="">All statuses</option>
            <option value="pending">Pending</option>
            <option value="running">Running</option>
            <option value="done">Done</option>
            <option value="failed">Failed</option>
            <option value="cancelled">Cancelled</option>
          </select>
          <label className="text-xs text-gray-500 flex items-center gap-1">
            From
            <input type="date" value={fromDate} onChange={e => { setFromDate(e.target.value); setOffset(0) }}
              className="bg-white/5 border border-white/10 rounded-lg px-2 py-1.5 text-sm" />
          </label>
          <label className="text-xs text-gray-500 flex items-center gap-1">
            To
            <input type="date" value={toDate} onChange={e => { setToDate(e.target.value); setOffset(0) }}
              className="bg-white/5 border border-white/10 rounded-lg px-2 py-1.5 text-sm" />
          </label>
          {query && (
            <span className="text-xs text-gray-500">
              {filtered.length} of {jobs?.length ?? 0} on this page
            </span>
          )}
        </div>

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

        {groups.length > 0 && (
          <div className="space-y-5">
            {groups.map((g, gi) => {
              const isCollapsed = collapsed.has(g.owner)
              return (
                <Reveal key={g.owner} delay={gi * 40}>
                  <div className="border border-white/10 rounded-xl overflow-hidden">
                    <button
                      onClick={() => toggleGroup(g.owner)}
                      className="w-full flex items-center justify-between gap-3 px-4 py-3 bg-white/[0.03]
                        hover:bg-white/[0.05] transition-colors duration-100 text-left"
                    >
                      <span className="flex items-center gap-2 min-w-0">
                        <span className={`text-gray-500 text-xs transition-transform duration-150 ${isCollapsed ? '' : 'rotate-90'}`}>▶</span>
                        <span className="font-medium text-gray-100 truncate">{g.owner}</span>
                        <span className="text-xs text-gray-500 whitespace-nowrap">
                          {g.jobs.length} report{g.jobs.length === 1 ? '' : 's'}
                        </span>
                      </span>
                      <span className="flex items-center gap-3 text-xs whitespace-nowrap">
                        <span className="text-[#00C2BB]">{g.done} done</span>
                        {g.failed > 0 && <span className="text-orange-400">{g.failed} failed</span>}
                      </span>
                    </button>

                    {!isCollapsed && (
                      <table className="w-full text-sm">
                        <thead className="bg-white/[0.02] text-xs uppercase tracking-wider text-gray-500">
                          <tr>
                            <th className="text-left px-4 py-2">When</th>
                            <th className="text-left px-4 py-2">Prospect</th>
                            <th className="text-left px-4 py-2">Status</th>
                            <th className="text-right px-4 py-2">AWS spend (pre-tax)</th>
                            <th className="text-right px-4 py-2"></th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-white/5">
                          {g.jobs.map(j => (
                            <tr key={j.id} onClick={() => nav(`/jobs/${j.id}`)}
                                className="cursor-pointer hover:bg-white/[0.04] transition-colors duration-100">
                              <td className="px-4 py-3 text-gray-400 whitespace-nowrap">{fmtWhen(j.created_at)}</td>
                              <td className="px-4 py-3 text-gray-200 font-medium">{j.prospect}</td>
                              <td className={`px-4 py-3 ${statusColor[j.status] ?? 'text-gray-400'}`}>
                                <span className="flex items-center gap-1.5">
                                  <span className={`text-xs font-bold ${j.status === 'running' ? 'animate-pulse' : ''}`}>
                                    {statusIcon[j.status] ?? ''}
                                  </span>
                                  {j.status.charAt(0).toUpperCase() + j.status.slice(1)}
                                </span>
                                {j.status === 'failed' && j.error && (
                                  <p className="text-xs text-gray-500 mt-0.5 max-w-xs truncate" title={j.error}>
                                    {j.error}
                                  </p>
                                )}
                                {rowError[j.id] && (
                                  <p className="text-xs text-orange-400 mt-0.5">{rowError[j.id]}</p>
                                )}
                              </td>
                              <td className="px-4 py-3 text-right text-gray-300 tabular-nums">{fmtMoney(j.aws_spend)}</td>
                              <td className="px-4 py-3 text-right whitespace-nowrap">
                                <div className="flex items-center justify-end gap-3">
                                  {j.status === 'failed' && (
                                    <button
                                      onClick={e => handleRetry(e, j.id)}
                                      disabled={busy.has(j.id)}
                                      className="text-xs font-medium text-[#645DF6] hover:text-[#8981ff]
                                        disabled:opacity-40 disabled:cursor-not-allowed transition-colors duration-150"
                                    >
                                      {busy.has(j.id) ? '…' : 'Retry'}
                                    </button>
                                  )}
                                  {(j.status === 'running' || j.status === 'pending') && (
                                    <button
                                      onClick={e => handleCancel(e, j.id)}
                                      disabled={busy.has(j.id)}
                                      className="text-xs font-medium text-orange-400 hover:text-orange-300
                                        disabled:opacity-40 disabled:cursor-not-allowed transition-colors duration-150"
                                    >
                                      {busy.has(j.id) ? '…' : 'Cancel'}
                                    </button>
                                  )}
                                  {(j.status === 'done' || j.status === 'failed' || j.status === 'cancelled') && (
                                    <button
                                      onClick={e => handleDelete(e, j.id)}
                                      disabled={busy.has(j.id)}
                                      className="text-xs font-medium text-gray-500 hover:text-red-400
                                        disabled:opacity-40 disabled:cursor-not-allowed transition-colors duration-150"
                                    >
                                      {busy.has(j.id) ? '…' : 'Delete'}
                                    </button>
                                  )}
                                </div>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                </Reveal>
              )
            })}
          </div>
        )}

        {total > PAGE_SIZE && (
          <div className="flex items-center justify-center gap-4 mt-8 text-sm">
            <button
              onClick={() => setOffset(o => Math.max(0, o - PAGE_SIZE))}
              disabled={offset === 0}
              className="px-3 py-1.5 rounded-lg border border-white/10 text-gray-300 disabled:opacity-30 hover:bg-white/5"
            >
              ← Prev
            </button>
            <span className="text-gray-500 text-xs">Page {page} of {pageCount} · {total} total</span>
            <button
              onClick={() => setOffset(o => o + PAGE_SIZE)}
              disabled={offset + PAGE_SIZE >= total}
              className="px-3 py-1.5 rounded-lg border border-white/10 text-gray-300 disabled:opacity-30 hover:bg-white/5"
            >
              Next →
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
