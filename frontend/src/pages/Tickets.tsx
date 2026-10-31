import { useEffect, useState } from 'react'
import { Layout } from '../components/Layout'
import { createTicket, listTickets, updateTicket, type Ticket } from '../api/admin'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'
import { toast } from 'sonner'
import { BackButton } from '../components/BackButton'

function statusColor(status: string): string {
  switch (status) {
    case 'open': return 'text-[#00C2BB]'
    case 'in_progress': return 'text-[#645DF6]'
    case 'resolved': return 'text-[#10B981]'
    case 'closed': return 'text-gray-400'
    default: return 'text-gray-400'
  }
}

function priorityColor(priority: string): string {
  switch (priority) {
    case 'high': return 'text-red-400'
    case 'medium': return 'text-orange-400'
    case 'low': return 'text-gray-400'
    default: return 'text-gray-400'
  }
}

export function Tickets({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Support tickets')
  const [tickets, setTickets] = useState<Ticket[]>([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [updating, setUpdating] = useState<string | null>(null)
  const [newTitle, setNewTitle] = useState('')
  const [newDescription, setNewDescription] = useState('')
  const [newPriority, setNewPriority] = useState('medium')
  const [newCategory, setNewCategory] = useState('general')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editTitle, setEditTitle] = useState('')
  const [editDescription, setEditDescription] = useState('')
  const [editStatus, setEditStatus] = useState('open')
  const [editPriority, setEditPriority] = useState('medium')
  const [editCategory, setEditCategory] = useState('general')
  const [error, setError] = useState<string | null>(null)

  const isAdmin = Boolean(user?.is_admin)

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await listTickets()
      setTickets(res.tickets)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to load tickets')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newTitle.trim() || !newDescription.trim()) return
    setCreating(true)
    setError(null)
    try {
      await createTicket({
        title: newTitle.trim(),
        description: newDescription.trim(),
        priority: newPriority,
        category: newCategory,
      })
      setNewTitle('')
      setNewDescription('')
      setNewPriority('medium')
      setNewCategory('general')
      await refresh()
      toast.success('Ticket created')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to create ticket')
    } finally {
      setCreating(false)
    }
  }

  const openEdit = (ticket: Ticket) => {
    setEditingId(ticket.id)
    setEditTitle(ticket.title)
    setEditDescription(ticket.description)
    setEditStatus(ticket.status)
    setEditPriority(ticket.priority)
    setEditCategory(ticket.category)
  }

  const handleUpdate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!editingId) return
    setUpdating(editingId)
    setError(null)
    try {
      await updateTicket(editingId, {
        title: editTitle.trim() || undefined,
        description: editDescription.trim() || undefined,
        status: editStatus,
        priority: editPriority,
        category: editCategory,
      })
      setEditingId(null)
      await refresh()
      toast.success('Ticket updated')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to update ticket')
    } finally {
      setUpdating(null)
    }
  }

  const filtered = tickets

  return (
    <Layout user={user}>
      <div className="max-w-5xl mx-auto px-6 py-8">
        <BackButton fallback={isAdmin ? "/admin" : "/"} />
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-6">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Support Tickets</h1>
            <p className="text-sm text-gray-400">
              Raise a ticket for anything — billing questions, report issues, feature requests, or general support.
            </p>
          </div>
        </div>

        {error && (
          <div className="mb-4 p-3 rounded-lg bg-orange-950/40 border border-orange-500/40 text-orange-400 text-xs">
            {error}
          </div>
        )}

        {/* Create Ticket Form */}
        <div className="bg-white/[0.02] border border-white/10 rounded-xl p-6 mb-8">
          <h2 className="text-sm font-semibold text-white mb-4">Raise a New Ticket</h2>
          <form onSubmit={handleCreate} className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <div className="col-span-full sm:col-span-2 lg:col-span-2">
              <label className="block text-xs text-gray-400 mb-1">Title</label>
              <input
                type="text"
                value={newTitle}
                onChange={e => setNewTitle(e.target.value)}
                placeholder="What do you need help with?"
                className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:border-[#645DF6] transition-colors"
              />
            </div>
            <div className="col-span-full sm:col-span-2 lg:col-span-2">
              <label className="block text-xs text-gray-400 mb-1">Description</label>
              <textarea
                value={newDescription}
                onChange={e => setNewDescription(e.target.value)}
                placeholder="Describe your issue or request in detail..."
                rows={3}
                className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:border-[#645DF6] transition-colors"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">Priority</label>
              <select
                value={newPriority}
                onChange={e => setNewPriority(e.target.value)}
                className="w-full bg-[#0e0e14] border border-white/20 rounded-lg px-3 py-2 text-sm text-white focus:border-[#645DF6] transition-colors cursor-pointer"
              >
                <option value="low" className="bg-[#0e0e14] text-white">Low</option>
                <option value="medium" className="bg-[#0e0e14] text-white">Medium</option>
                <option value="high" className="bg-[#0e0e14] text-white">High</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">Category</label>
              <select
                value={newCategory}
                onChange={e => setNewCategory(e.target.value)}
                className="w-full bg-[#0e0e14] border border-white/20 rounded-lg px-3 py-2 text-sm text-white focus:border-[#645DF6] transition-colors cursor-pointer"
              >
                <option value="general" className="bg-[#0e0e14] text-white">General</option>
                <option value="billing" className="bg-[#0e0e14] text-white">Billing</option>
                <option value="technical" className="bg-[#0e0e14] text-white">Technical</option>
                <option value="feature" className="bg-[#0e0e14] text-white">Feature Request</option>
              </select>
            </div>
            <div className="col-span-full sm:col-span-2 lg:col-span-2">
              <button
                type="submit"
                disabled={creating}
                className="w-full btn-shimmer py-2 rounded-lg text-sm font-medium text-white cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {creating ? 'Creating…' : 'Raise Ticket'}
              </button>
            </div>
          </form>
        </div>

        {/* Tickets List */}
        <div className="bg-white/[0.02] border border-white/10 rounded-xl overflow-hidden">
          {loading ? (
            <div className="p-8 text-center text-gray-400 text-sm">Loading tickets…</div>
          ) : filtered.length === 0 ? (
            <div className="p-8 text-center text-gray-400 text-sm">No tickets yet.</div>
          ) : (
            <table className="w-full text-sm">
              <thead className="bg-white/[0.04] text-xs uppercase tracking-wider text-gray-400 border-b border-white/10">
                <tr>
                  <th className="text-left px-5 py-3 font-medium">ID</th>
                  <th className="text-left px-5 py-3 font-medium">Title</th>
                  <th className="text-left px-5 py-3 font-medium">Status</th>
                  <th className="text-left px-5 py-3 font-medium">Priority</th>
                  <th className="text-left px-5 py-3 font-medium">Category</th>
                  <th className="text-left px-5 py-3 font-medium">Created By</th>
                  <th className="text-left px-5 py-3 font-medium">Created At</th>
                  <th className="text-right px-5 py-3 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {filtered.map(t => (
                  <tr key={t.id} className="hover:bg-white/[0.02] transition-colors">
                    <td className="px-5 py-3 text-xs font-mono text-gray-400 whitespace-nowrap">{t.id}</td>
                    <td className="px-5 py-3 text-gray-200 font-medium">{t.title}</td>
                    <td className="px-5 py-3">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${statusColor(t.status)} border-current`}>
                        {t.status}
                      </span>
                    </td>
                    <td className="px-5 py-3">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${priorityColor(t.priority)} border-current`}>
                        {t.priority}
                      </span>
                    </td>
                    <td className="px-5 py-3 text-xs text-gray-400 capitalize">{t.category}</td>
                    <td className="px-5 py-3 text-xs text-gray-400">{t.created_by}</td>
                    <td className="px-5 py-3 text-xs text-gray-400 whitespace-nowrap">
                      {new Date(t.created_at).toLocaleString()}
                    </td>
                    <td className="px-5 py-3 text-right whitespace-nowrap">
                      <div className="flex items-center justify-end gap-2">
                        <button
                          onClick={() => openEdit(t)}
                          className="text-xs px-2 py-1 rounded border border-white/10 hover:border-white/30 text-gray-300 hover:text-white transition-colors cursor-pointer"
                        >
                          Edit
                        </button>
                        {t.status !== 'resolved' && t.status !== 'closed' && isAdmin && (
                          <button
                            onClick={() => {
                              if (!confirm(`Mark ticket "${t.title}" as resolved?`)) return
                              updateTicket(t.id, { status: 'resolved' })
                                .then(() => refresh())
                                .catch(err => toast.error(err instanceof Error ? err.message : 'failed'))
                            }}
                            className="text-xs px-2 py-1 rounded bg-[#10B981]/20 hover:bg-[#10B981]/40 text-[#10B981] transition-colors cursor-pointer"
                          >
                            Resolve
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

        {/* Edit Ticket Dialog */}
        {editingId && (
          <div
            role="dialog"
            aria-modal="true"
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm px-4 anim-fade-in"
            onClick={e => {
              if ((e.target as HTMLElement).className.includes('fixed inset-0')) setEditingId(null)
            }}
          >
            <div className="bg-[#0d0d14] border border-white/10 rounded-2xl shadow-2xl p-6 w-full max-w-md anim-scale-in">
              <div className="flex items-start justify-between gap-4 mb-4">
                <div>
                  <h2 className="text-lg font-semibold text-white">Edit Ticket</h2>
                  <p className="text-xs text-gray-400">{editingId}</p>
                </div>
                <button
                  onClick={() => setEditingId(null)}
                  className="text-gray-500 hover:text-gray-200 text-xl leading-none transition-colors duration-150"
                  aria-label="Close">
                  ×
                </button>
              </div>
              <form onSubmit={handleUpdate} className="space-y-4">
                <div>
                  <label className="block text-xs text-gray-400 mb-1">Title</label>
                  <input
                    type="text"
                    value={editTitle}
                    onChange={e => setEditTitle(e.target.value)}
                    className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:border-[#645DF6] transition-colors"
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1">Description</label>
                  <textarea
                    value={editDescription}
                    onChange={e => setEditDescription(e.target.value)}
                    rows={4}
                    className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:border-[#645DF6] transition-colors"
                  />
                </div>
                <div className="grid grid-cols-3 gap-4">
                  <div>
                    <label className="block text-xs text-gray-400 mb-1">Status</label>
                    <select
                      value={editStatus}
                      onChange={e => setEditStatus(e.target.value)}
                      disabled={!isAdmin}
                      className="w-full bg-[#0e0e14] border border-white/20 rounded-lg px-3 py-2 text-sm text-white focus:border-[#645DF6] transition-colors disabled:opacity-50 cursor-pointer"
                    >
                      <option value="open" className="bg-[#0e0e14] text-white">Open</option>
                      <option value="in_progress" className="bg-[#0e0e14] text-white">In Progress</option>
                      <option value="resolved" className="bg-[#0e0e14] text-white">Resolved</option>
                      <option value="closed" className="bg-[#0e0e14] text-white">Closed</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs text-gray-400 mb-1">Priority</label>
                    <select
                      value={editPriority}
                      onChange={e => setEditPriority(e.target.value)}
                      className="w-full bg-[#0e0e14] border border-white/20 rounded-lg px-3 py-2 text-sm text-white focus:border-[#645DF6] transition-colors cursor-pointer"
                    >
                      <option value="low" className="bg-[#0e0e14] text-white">Low</option>
                      <option value="medium" className="bg-[#0e0e14] text-white">Medium</option>
                      <option value="high" className="bg-[#0e0e14] text-white">High</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs text-gray-400 mb-1">Category</label>
                    <select
                      value={editCategory}
                      onChange={e => setEditCategory(e.target.value)}
                      className="w-full bg-[#0e0e14] border border-white/20 rounded-lg px-3 py-2 text-sm text-white focus:border-[#645DF6] transition-colors cursor-pointer"
                    >
                      <option value="general" className="bg-[#0e0e14] text-white">General</option>
                      <option value="billing" className="bg-[#0e0e14] text-white">Billing</option>
                      <option value="technical" className="bg-[#0e0e14] text-white">Technical</option>
                      <option value="feature" className="bg-[#0e0e14] text-white">Feature Request</option>
                    </select>
                  </div>
                </div>
                <div className="flex items-center justify-end gap-3 pt-4 border-t border-white/10">
                  <button
                    type="button"
                    onClick={() => setEditingId(null)}
                    className="px-4 py-2 text-sm text-gray-400 hover:text-gray-200 transition-colors cursor-pointer"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={updating === editingId}
                    className="px-5 py-2 rounded-lg text-sm font-medium text-white btn-shimmer cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {updating === editingId ? 'Saving…' : 'Save Changes'}
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}
      </div>
    </Layout>
  )
}
