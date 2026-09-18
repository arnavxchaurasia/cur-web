import { useEffect, useState } from 'react'
import { Nav } from '../components/Nav'
import { getCatalogStatus, triggerCatalogRefresh } from '../api/admin'
import type { CatalogStatus } from '../api/admin'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'

export function AdminCatalog({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Catalog — admin')
  const [status, setStatus] = useState<CatalogStatus | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = () => getCatalogStatus().then(setStatus)
  useEffect(() => { refresh() }, [])

  const handleRefresh = async () => {
    setRefreshing(true)
    setError(null)
    try {
      await triggerCatalogRefresh()
      setTimeout(refresh, 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'refresh failed')
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <div className="min-h-screen text-white">
      <Nav user={user} />
      <div className="max-w-2xl mx-auto px-6 py-10">
        <h1 className="text-2xl font-semibold mb-1">GCP catalog</h1>
        <p className="text-sm text-gray-500 mb-8">Freshness of the GCP SKU pricing catalog used for projections.</p>

        {status === null && <p className="text-sm text-gray-500">Loading…</p>}

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
            {error && <p className="text-xs text-orange-400">{error}</p>}
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
      </div>
    </div>
  )
}
