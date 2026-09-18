import { useEffect, useState } from 'react'
import { Nav } from '../components/Nav'
import { listAuditLog } from '../api/admin'
import type { AuditEntry } from '../api/admin'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'

const PAGE_SIZE = 50

export function AdminAudit({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Audit log — admin')
  const [entries, setEntries] = useState<AuditEntry[] | null>(null)
  const [offset, setOffset] = useState(0)

  useEffect(() => {
    listAuditLog({ limit: PAGE_SIZE, offset }).then(setEntries)
  }, [offset])

  return (
    <div className="min-h-screen text-white">
      <Nav user={user} />
      <div className="max-w-5xl mx-auto px-6 py-10">
        <h1 className="text-2xl font-semibold mb-1">Audit log</h1>
        <p className="text-sm text-gray-500 mb-8">Every admin action, most recent first.</p>

        {entries === null && <p className="text-sm text-gray-500 py-12 text-center">Loading…</p>}
        {entries !== null && entries.length === 0 && <p className="text-sm text-gray-500 py-12 text-center">No admin actions logged yet.</p>}

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
    </div>
  )
}
