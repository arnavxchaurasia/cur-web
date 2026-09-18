import { useEffect, useState } from 'react'
import { Nav } from '../components/Nav'
import { listUsers, setUserRole } from '../api/admin'
import type { AdminUser } from '../api/admin'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'

const ROLES = ['viewer', 'admin', 'facets']

export function AdminUsers({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Users — admin')
  const [users, setUsers] = useState<AdminUser[] | null>(null)
  const [busy, setBusy] = useState<Set<string>>(new Set())
  const [error, setError] = useState<Record<string, string>>({})

  const refresh = () => listUsers().then(setUsers)
  useEffect(() => { refresh() }, [])

  const handleChange = async (email: string, role: string) => {
    setBusy(prev => new Set(prev).add(email))
    setError(prev => { const next = { ...prev }; delete next[email]; return next })
    try {
      await setUserRole(email, role)
      await refresh()
    } catch (err) {
      setError(prev => ({ ...prev, [email]: err instanceof Error ? err.message : 'update failed' }))
    } finally {
      setBusy(prev => { const next = new Set(prev); next.delete(email); return next })
    }
  }

  return (
    <div className="min-h-screen text-white">
      <Nav user={user} />
      <div className="max-w-4xl mx-auto px-6 py-10">
        <h1 className="text-2xl font-semibold mb-1">Users</h1>
        <p className="text-sm text-gray-500 mb-8">Manage RBAC roles for everyone who has logged in.</p>

        {users === null && <p className="text-sm text-gray-500 py-12 text-center">Loading…</p>}
        {users !== null && users.length === 0 && <p className="text-sm text-gray-500 py-12 text-center">No users yet.</p>}

        {users && users.length > 0 && (
          <div className="border border-white/10 rounded-xl overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-white/[0.02] text-xs uppercase tracking-wider text-gray-500">
                <tr>
                  <th className="text-left px-4 py-2">Email</th>
                  <th className="text-left px-4 py-2">Role</th>
                  <th className="text-left px-4 py-2">Last seen</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {users.map(u => (
                  <tr key={u.email}>
                    <td className="px-4 py-3 text-gray-200">{u.email}</td>
                    <td className="px-4 py-3">
                      <select
                        value={u.role}
                        disabled={busy.has(u.email)}
                        onChange={e => handleChange(u.email, e.target.value)}
                        className="bg-white/5 border border-white/10 rounded-lg px-2 py-1 text-sm disabled:opacity-40"
                      >
                        {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
                      </select>
                      {error[u.email] && <p className="text-xs text-orange-400 mt-1">{error[u.email]}</p>}
                    </td>
                    <td className="px-4 py-3 text-gray-500 text-xs">{new Date(u.updated_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
