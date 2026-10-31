import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Layout } from '../components/Layout'
import {
  getCatalogStatus,
  triggerCatalogRefresh,
  getNotifyStatus,
  notifyTest,
  type CatalogStatus,
  type NotifyStatus,
} from '../api/admin'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'
import { parseAdminApiError, type AdminApiErrorState } from '../components/adminError'
import { BackButton } from '../components/BackButton'

export function AdminCatalog({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Catalog — admin')
  const nav = useNavigate()
  const [status, setStatus] = useState<CatalogStatus | null>(null)
  const [notifyStatus, setNotifyStatus] = useState<NotifyStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [notifying, setNotifying] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<AdminApiErrorState | null>(null)

  const refresh = async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const [s, n] = await Promise.all([getCatalogStatus(), getNotifyStatus()])
      setStatus(s)
      setNotifyStatus(n)
    } catch (err) {
      const parsed = parseAdminApiError(err, 'Catalog & Notification Status', () => nav('/login'))
      setLoadError(parsed)
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { refresh() }, [])

  const handleRefresh = async () => {
    setRefreshing(true)
    setActionError(null)
    try {
      await triggerCatalogRefresh()
      setTimeout(refresh, 3000)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'refresh failed')
    } finally {
      setRefreshing(false)
    }
  }

  const handleNotifyTest = async () => {
    setNotifying(true)
    setActionError(null)
    try {
      await notifyTest()
      setTimeout(refresh, 1000)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'test notification failed')
    } finally {
      setNotifying(false)
    }
  }

  return (
    <Layout user={user}>
      <div className="max-w-2xl mx-auto px-6 py-10">
        <BackButton fallback="/admin" />
        <h1 className="text-2xl font-semibold mb-1">GCP catalog</h1>
        <p className="text-sm text-gray-500 mb-8">Freshness of the GCP SKU pricing catalog used for projections.</p>

        {loading && <p className="text-sm text-gray-500 py-12 text-center">Loading…</p>}

        {loadError && (
          <div className="p-4 rounded-xl bg-orange-950/40 border border-orange-500/40 text-orange-400 text-sm mb-6">
            <strong className="block font-semibold mb-1">
              {loadError.status === 403 ? 'Permission Denied' : 'Failed to load catalog status'}
            </strong>
            <p className="text-xs text-orange-300">{loadError.message}</p>
            <button
              onClick={refresh}
              className="mt-3 px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-white text-xs font-medium border border-white/10 transition-colors"
            >
              Retry
            </button>
          </div>
        )}

        {status && (
          <div className="bg-white/[0.03] border border-white/10 rounded-lg p-6 space-y-4">
            <div className="flex items-center justify-between">
              <span className="text-gray-400 text-sm">Last refreshed</span>
              <span className="font-semibold tabular-nums">
                {status.age_days >= 9999 ? 'unknown' : `${status.age_days} day${status.age_days === 1 ? '' : 's'} ago`}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-gray-400 text-sm">Status</span>
              <span className={`font-semibold ${status.stale ? 'text-orange-400' : 'text-[#00C2BB]'}`}>
                {status.stale ? 'Stale' : 'Fresh'}
              </span>
            </div>
            {actionError && <p className="text-xs text-orange-400">{actionError}</p>}
            <button
              onClick={handleRefresh}
              disabled={refreshing}
              className="btn-shimmer w-full py-2.5 rounded-lg font-medium text-white text-sm
                disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {refreshing ? 'Starting refresh…' : 'Refresh now'}
            </button>
          </div>
        )}

        {/* Notification health panel */}
        <div className="bg-white/[0.03] border border-white/10 rounded-lg p-6 space-y-4 mt-8">
          <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-4">
            Notification health
          </h2>
          {notifyStatus && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div className="px-4 py-3 rounded bg-white/[0.05] border border-white/10">
                  <div className="text-xs text-gray-400 uppercase tracking-wider mb-1">
                    Email notifications
                  </div>
                  <div className="flex items-center justify-between">
                    <span className={`text-sm ${notifyStatus.email.configured ? 'text-[#00C2BB]' : 'text-orange-400'}`}>
                      {notifyStatus.email.configured ? 'Configured' : 'Not configured'}
                    </span>
                  </div>
                </div>
                <div className="px-4 py-3 rounded bg-white/[0.05] border border-white/10">
                  <div className="text-xs text-gray-400 uppercase tracking-wider mb-1">
                    Slack notifications
                  </div>
                  <div className="flex items-center justify-between">
                    <span className={`text-sm ${notifyStatus.slack.configured ? 'text-[#00C2BB]' : 'text-orange-400'}`}>
                      {notifyStatus.slack.configured ? 'Configured' : 'Not configured'}
                    </span>
                  </div>
                </div>
              </div>
              <div className="flex gap-3">
                <button
                  onClick={handleNotifyTest}
                  disabled={notifying}
                  className="px-4 py-2 rounded text-sm text-xs font-medium text-[#645DF6] hover:bg-[#645DF6]/10 disabled:opacity-40 transition-colors"
                >
                  {notifying ? 'Testing…' : 'Send test notification'}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </Layout>
  )
}