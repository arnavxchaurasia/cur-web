import { useEffect, useState, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { Layout } from '../components/Layout'
import { listUsers, setUserRole } from '../api/admin'
import type { AdminUser } from '../api/admin'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'
import { BackButton } from '../components/BackButton'
import { toast } from 'sonner'

export function AdminUsers({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Users — admin')
  const [users, setUsers] = useState<AdminUser[] | null>(null)
  const [search, setSearch] = useState('')
  const [busy, setBusy] = useState<Set<string>>(new Set())
  const [error, setError] = useState<Record<string, string>>({})
  const [globalError, setGlobalError] = useState<string | null>(null)

  // Confirmation dialog state
  const [confirm, setConfirm] = useState<{
    email: string
    fromRole: string
    toRole: string
    severity: 'grant_admin' | 'revoke_admin' | 'other'
    isExternal: boolean
  } | null>(null)

  const refresh = () => listUsers().then(setUsers).catch(err => {
    setGlobalError(err instanceof Error ? err.message : 'load failed')
  })
  useEffect(() => { refresh() }, [])

  const filteredUsers = useMemo(() => {
    if (!users) return null
    const q = search.trim().toLowerCase()
    if (!q) return users
    return users.filter(u => u.email.toLowerCase().includes(q) || u.role.toLowerCase().includes(q))
  }, [users, search])

  const isExternalEmail = (email: string) => {
    const lower = email.toLowerCase()
    return !lower.endsWith('@facets.cloud') && !lower.endsWith('@facets.io') && !lower.includes('@facets.')
  }

  const openConfirm = (email: string, fromRole: string, toRole: string) => {
    const isExternal = isExternalEmail(email)
    let severity: 'grant_admin' | 'revoke_admin' | 'other' = 'other'
    if (toRole === 'admin') severity = 'grant_admin'
    else if (fromRole === 'admin' && toRole !== 'admin') severity = 'revoke_admin'
    setConfirm({ email, fromRole, toRole, severity, isExternal })
    setError(prev => { const next = { ...prev }; delete next[email]; return next })
  }

  const closeConfirm = () => setConfirm(null)

  const severityTitle = (c: typeof confirm) => {
    if (!c) return ''
    if (c.severity === 'grant_admin') return `Grant admin access to \`${c.email}\`?`
    if (c.severity === 'revoke_admin') return `Remove admin access from \`${c.email}\`?`
    return `Change \`${c.email}\` from ${c.fromRole} to ${c.toRole}?`
  }

  const severityBody = (c: typeof confirm) => {
    if (!c) return ''
    if (c.severity === 'grant_admin') {
      return (
        <>
          <p>Admins can see and download <strong>every customer's bills and reports</strong>, change pricing assumptions, and promote other users.</p>
          {c.isExternal && <p>This user is outside Facets.</p>}
        </>
      )
    }
    if (c.severity === 'revoke_admin') {
      return <p>They will keep access to their own jobs only.</p>
    }
    return <p>The dropdown does not change until you confirm.</p>
  }

  const confirmButtonLabel = (c: typeof confirm) => {
    if (!c) return 'Confirm'
    if (c.severity === 'grant_admin') return 'Grant admin'
    if (c.severity === 'revoke_admin') return 'Remove admin'
    return 'Confirm change'
  }

  const handleChange = async (email: string, role: string) => {
    const u = users?.find(x => x.email === email)
    if (!u || u.role === role) return
    openConfirm(email, u.role, role)
  }

  const handleConfirm = async () => {
    if (!confirm) return
    const { email, toRole } = confirm
    setBusy(prev => new Set(prev).add(email))
    setError(prev => { const next = { ...prev }; delete next[email]; return next })
    try {
      await setUserRole(email, toRole)
      await refresh()
      toast.success(`Updated ${email} to ${toRole}`)
      closeConfirm()
    } catch (err) {
      setError(prev => ({ ...prev, [email]: err instanceof Error ? err.message : 'update failed' }))
      // Do not close the dialog on failure so the operator can re-read the email.
    } finally {
      setBusy(prev => { const next = new Set(prev); next.delete(email); return next })
    }
  }

  return (
    <Layout user={user}>
      <div className="max-w-5xl mx-auto px-6 py-10">
        <BackButton fallback="/admin" />
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-6">
          <div>
            <h1 className="text-2xl font-semibold mb-1">Users</h1>
            <p className="text-sm text-gray-400">Search users, view individual runs, and manage RBAC roles.</p>
          </div>
          <div className="flex items-center gap-3">
            <input
              type="search"
              placeholder="Search user or role…"
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-[#645DF6] transition-colors w-64"
            />
          </div>
        </div>

        {globalError && (
          <div className="mb-4 p-3 rounded-lg bg-orange-950/40 border border-orange-500/40 text-orange-400 text-xs">
            {globalError}
          </div>
        )}
        {users === null && <p className="text-sm text-gray-500 py-12 text-center">Loading…</p>}
        {users !== null && users.length === 0 && <p className="text-sm text-gray-500 py-12 text-center">No users registered yet.</p>}
        {filteredUsers !== null && users !== null && users.length > 0 && filteredUsers.length === 0 && (
          <p className="text-sm text-gray-500 py-12 text-center">No users matching &ldquo;{search}&rdquo;</p>}
        )}

        {filteredUsers && filteredUsers.length > 0 && (
          <div className="border border-white/10 rounded-xl overflow-hidden shadow-sm">
            <table className="w-full text-sm">
              <thead className="bg-white/[0.04] text-xs uppercase tracking-wider text-gray-400 border-b border-white/10">
                <tr>
                  <th className="text-left px-5 py-3 font-medium">User Email</th>
                  <th className="text-left px-5 py-3 font-medium">Role</th>
                  <th className="text-left px-5 py-3 font-medium">Last Seen</th>
                  <th className="text-right px-5 py-3 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {filteredUsers.map(u => {
                  const lowerEmail = u.email.toLowerCase()
                  const isFacets = lowerEmail.endsWith('@facets.cloud') || lowerEmail.endsWith('@facets.io') || lowerEmail.includes('@facets.')
                  const emailHasEncoding = u.email.includes('%')

                  return (
                    <tr key={u.email} className="hover:bg-white/[0.02] transition-colors">
                      <td className="px-5 py-3 text-gray-200 font-mono text-xs">
                        <div className="flex items-center gap-2">
                          {emailHasEncoding ? (
                            <span className="flex items-center gap-1 text-xs text-orange-400 font-mono">
                              {u.email}
                              <span
                                title="Stored email still contains URL-encoding (%); this is a ghost row from the old role route"
                                className="px-1.5 py-0.5 rounded bg-orange-950/20 text-orange-400 border border-orange-500/30 font-sans"
                              >
                                encoded
                              </span>
                            </span>
                          ) : (
                            <span>{u.email}</span>
                          )}
                          {isFacets && (
                            <span
                              title="@facets accounts are permanently admin"
                              className="px-1.5 py-0.5 rounded text-[10px] bg-[#645DF6]/15 text-[#8680FF] border border-[#645DF6]/30 font-sans"
                            >
                              facets core
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-5 py-3">
                        {isFacets ? (
                          <div className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-white/5 border border-white/10 text-xs text-gray-300">
                            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
                            <span className="font-medium text-emerald-400">admin</span>
                            <span className="text-[10px] text-gray-500 ml-1">(locked)</span>
                          </div>
                        ) : (
                          <div className="space-y-1">
                            <select
                              value={u.role}
                              disabled={busy.has(u.email) || confirm?.email === u.email}
                              onChange={e => handleChange(u.email, e.target.value)}
                              className="bg-white/5 border border-white/10 rounded-lg px-2.5 py-1 text-xs text-gray-200 disabled:opacity-40 focus:outline-none focus:border-[#645DF6] cursor-pointer"
                            >
                              <option value="viewer" className="bg-gray-900 text-white">viewer</option>
                              <option value="admin" className="bg-gray-900 text-white">admin</option>
                            </select>
                            {u.role === 'admin' && u.admin_granted_by && (
                              <p className="text-[11px] text-gray-400 font-sans">
                                admin (granted by <span className="text-[#00C2BB] font-mono">{u.admin_granted_by}</span>
                                {u.admin_granted_at ? `, ${u.admin_granted_at.slice(0, 10)}` : ''})
                              </p>
                            )}
                          </div>
                        )}
                        {error[u.email] && <p className="text-xs text-orange-400 mt-1">{error[u.email]}</p>}
                      </td>
                      <td className="px-5 py-3 text-gray-400 text-xs">{new Date(u.updated_at).toLocaleString()}</td>
                      <td className="px-5 py-3 text-right">
                        <Link
                          to={`/admin?q=${encodeURIComponent(u.email)}`}
                          className="inline-flex items-center gap-1.5 px-3 py-1 rounded bg-[#00C2BB]/10 text-[#00C2BB] hover:bg-[#00C2BB]/20 text-xs font-medium transition-colors"
                        >
                          View runs →
                        </Link>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Confirmation dialog */}
        {confirm && (
          <div className="fixed inset-0 z-50 flex items-center justify-center">
            <div className="fixed inset-0 bg-black/60 backdrop-blur-sm" onClick={closeConfirm} />
            <div className="bg-white/10 border border-white/20 rounded-xl p-6 shadow-2xl w-full max-w-md relative z-10">
              <div className="flex items-start gap-3 mb-4">
                <div className={`mt-1 p-1.5 rounded-lg ${
                  confirm.severity === 'grant_admin' ? 'bg-red-950/20 text-red-400 border border-red-500/30' :
                  confirm.severity === 'revoke_admin' ? 'bg-orange-950/20 text-orange-400 border border-orange-500/30' :
                  'bg-blue-950/20 text-blue-400 border border-blue-500/30'
                }`}>
                  {confirm.severity === 'grant_admin' && '⚠️'}
                  {confirm.severity === 'revoke_admin' && '🔒'}
                  {confirm.severity === 'other' && 'ℹ️'}
                </div>
                <div>
                  <h3 className="text-sm font-semibold text-white">{severityTitle(confirm)}</h3>
                  <p className="text-xs text-gray-400 mt-1 leading-relaxed">{severityBody(confirm)}</p>
                </div>
              </div>
              <div className="flex justify-end gap-2 mt-6">
                <button
                  onClick={closeConfirm}
                  autoFocus
                  className="px-4 py-2 rounded-lg text-sm font-medium text-gray-300 hover:text-white hover:bg-white/5 transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleConfirm}
                  disabled={busy.has(confirm.email)}
                  className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                    confirm.severity === 'grant_admin'
                      ? 'bg-red-600 text-white hover:bg-red-500 disabled:opacity-50 disabled:cursor-not-allowed'
                      : confirm.severity === 'revoke_admin'
                        ? 'bg-orange-600 text-white hover:bg-orange-500 disabled:opacity-50 disabled:cursor-not-allowed'
                        : 'bg-[#645DF6] text-white hover:bg-[#645DF6]/80 disabled:opacity-50 disabled:cursor-not-allowed'
                  }`}
                >
                  {busy.has(confirm.email) ? 'Saving…' : confirmButtonLabel(confirm)}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </Layout>
  )
}
