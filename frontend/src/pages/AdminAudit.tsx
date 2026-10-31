import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Layout } from '../components/Layout'
import { listAuditLog } from '../api/admin'
import type { AuditEntry } from '../api/admin'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'
import { parseAdminApiError, type AdminApiErrorState } from '../components/adminError'
import { BackButton } from '../components/BackButton'

const PAGE_SIZE = 50

export function AdminAudit({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Audit log — admin')
  const nav = useNavigate()
  const [entries, setEntries] = useState<AuditEntry[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<AdminApiErrorState | null>(null)
  const [offset, setOffset] = useState(0)

  // Filter states
  const [actorFilter, setActorFilter] = useState('')
  const [actionFilter, setActionFilter] = useState('')
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')

  const loadData = () => {
    setLoading(true)
    setLoadError(null)
    listAuditLog({
      limit: PAGE_SIZE,
      offset,
      actor: actorFilter.trim() || undefined,
      action: actionFilter.trim() || undefined,
      from: fromDate || undefined,
      to: toDate || undefined,
    })
      .then(res => setEntries(res))
      .catch(err => {
        const parsed = parseAdminApiError(err, 'Audit Log', () => nav('/login'))
        setLoadError(parsed)
      })
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    const timer = setTimeout(() => {
      loadData()
    }, 300)
    return () => clearTimeout(timer)
  }, [offset, actorFilter, actionFilter, fromDate, toDate])

  const handleFilterChange = (setter: React.Dispatch<React.SetStateAction<string>>, val: string) => {
    setter(val)
    setOffset(0)
  }

  return (
    <Layout user={user}>
      <div className="max-w-5xl mx-auto px-6 py-10">
        <BackButton fallback="/admin" />
        <h1 className="text-2xl font-semibold mb-1">Audit log</h1>
        <p className="text-sm text-gray-500 mb-6">Every admin action, most recent first.</p>

        {/* Filter Controls */}
        <div className="flex items-center gap-3 flex-wrap mb-6 bg-white/[0.02] border border-white/10 rounded-xl p-3 text-xs">
          <input
            type="text"
            value={actorFilter}
            onChange={e => handleFilterChange(setActorFilter, e.target.value)}
            placeholder="Filter by actor email…"
            className="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-xs text-gray-200 placeholder:text-gray-500 focus:border-[#645DF6] transition-colors"
          />
          <input
            type="text"
            value={actionFilter}
            onChange={e => handleFilterChange(setActionFilter, e.target.value)}
            placeholder="Filter by action (e.g. stop_all)…"
            className="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-xs text-gray-200 placeholder:text-gray-500 focus:border-[#645DF6] transition-colors"
          />
          <div className="flex items-center gap-1.5 text-gray-400">
            <span>From:</span>
            <input
              type="date"
              value={fromDate}
              onChange={e => handleFilterChange(setFromDate, e.target.value)}
              className="bg-white/5 border border-white/10 rounded-lg px-2 py-1 text-xs text-gray-300 focus:border-[#645DF6]"
            />
          </div>
          <div className="flex items-center gap-1.5 text-gray-400">
            <span>To:</span>
            <input
              type="date"
              value={toDate}
              onChange={e => handleFilterChange(setToDate, e.target.value)}
              className="bg-white/5 border border-white/10 rounded-lg px-2 py-1 text-xs text-gray-300 focus:border-[#645DF6]"
            />
          </div>
          {(actorFilter || actionFilter || fromDate || toDate) && (
            <button
              onClick={() => {
                setActorFilter('')
                setActionFilter('')
                setFromDate('')
                setToDate('')
                setOffset(0)
              }}
              className="px-2.5 py-1 text-gray-400 hover:text-white transition-colors cursor-pointer"
            >
              Clear filters
            </button>
          )}
        </div>

        {loading && <p className="text-sm text-gray-500 py-12 text-center">Loading…</p>}

        {loadError && (
          <div className="p-4 rounded-xl bg-orange-950/40 border border-orange-500/40 text-orange-400 text-sm mb-6">
            <strong className="block font-semibold mb-1">
              {loadError.status === 403 ? 'Permission Denied' : 'Failed to load audit log'}
            </strong>
            <p className="text-xs text-orange-300">{loadError.message}</p>
            <button
              onClick={loadData}
              className="mt-3 px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-white text-xs font-medium border border-white/10 transition-colors cursor-pointer"
            >
              Retry
            </button>
          </div>
        )}

        {!loading && !loadError && entries !== null && entries.length === 0 && (
          <p className="text-sm text-gray-500 py-12 text-center">No admin actions match the filter.</p>
        )}

        {entries && entries.length > 0 && (
          <div className="border border-white/10 rounded-xl overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-white/[0.02] text-xs uppercase tracking-wider text-gray-500">
                <tr>
                  <th className="text-left px-4 py-2">When</th>
                  <th className="text-left px-4 py-2">Actor</th>
                  <th className="text-left px-4 py-2">Action</th>
                  <th className="text-left px-4 py-2">Target</th>
                  <th className="text-left px-4 py-2">Details</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {entries.map(e => (
                  <tr key={e.id}>
                    <td className="px-4 py-3 text-gray-400 whitespace-nowrap text-xs">{new Date(e.created_at).toLocaleString()}</td>
                    <td className="px-4 py-3 text-gray-200">{e.actor_email}</td>
                    <td className="px-4 py-3 text-[#645DF6] font-medium">{e.action}</td>
                    <td className="px-4 py-3 text-gray-400 font-mono text-xs">{e.target_id || '—'}</td>
                    <td className="px-4 py-3 text-gray-500 text-xs max-w-xs truncate" title={e.details}>{e.details || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="flex items-center justify-center gap-4 mt-8 text-sm">
          <button
            onClick={() => setOffset(o => Math.max(0, o - PAGE_SIZE))}
            disabled={offset === 0}
            className="px-3 py-1.5 rounded-lg border border-white/10 text-gray-300 disabled:opacity-30 hover:bg-white/5"
          >
            ← Prev
          </button>
          <button
            onClick={() => setOffset(o => o + PAGE_SIZE)}
            disabled={!entries || entries.length < PAGE_SIZE}
            className="px-3 py-1.5 rounded-lg border border-white/10 text-gray-300 disabled:opacity-30 hover:bg-white/5"
          >
            Next →
          </button>
        </div>
      </div>
    </Layout>
  )
}
