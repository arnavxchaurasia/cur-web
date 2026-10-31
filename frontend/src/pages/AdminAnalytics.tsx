import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Layout } from '../components/Layout'
import { getAnalytics } from '../api/admin'
import type { UsageStats } from '../api/admin'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { parseAdminApiError, type AdminApiErrorState } from '../components/adminError'
import { BackButton } from '../components/BackButton'

export function AdminAnalytics({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Analytics — admin')
  const nav = useNavigate()
  const [stats, setStats] = useState<UsageStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<AdminApiErrorState | null>(null)

  const loadData = () => {
    setLoading(true)
    setLoadError(null)
    getAnalytics()
      .then(res => setStats(res))
      .catch(err => {
        const parsed = parseAdminApiError(err, 'Platform Analytics', () => nav('/login'))
        setLoadError(parsed)
      })
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    loadData()
  }, [])

  const chartData = (stats?.jobs_by_day ?? []).map(d => ({ day: d.day.slice(5), count: d.count }))

  return (
    <Layout user={user}>
      <div className="max-w-5xl mx-auto px-6 py-10">
        <BackButton fallback="/admin" />
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-8">
          <div>
            <h1 className="text-2xl font-semibold mb-1">Analytics</h1>
            <p className="text-sm text-gray-500">Platform-wide usage, throughput, and reliability metrics over the last 30 days.</p>
          </div>
          <button
            onClick={loadData}
            disabled={loading}
            className="self-start sm:self-auto inline-flex items-center gap-2 px-3.5 py-1.5 rounded-lg border border-white/10 hover:border-white/25 bg-white/5 hover:bg-white/10 text-xs font-medium text-gray-200 transition-colors cursor-pointer disabled:opacity-50"
          >
            <RefreshIcon className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>

        {loading && (
          <div className="flex flex-col items-center justify-center py-20 text-gray-500 gap-3">
            <div className="w-6 h-6 border-2 border-[#00C2BB] border-t-transparent rounded-full animate-spin" />
            <p className="text-xs">Loading analytics data…</p>
          </div>
        )}

        {loadError && (
          <div className="p-4 rounded-xl bg-red-950/30 border border-red-500/30 text-red-400 text-sm mb-6">
            <strong className="block font-semibold mb-1">
              {loadError.status === 403 ? 'Permission Denied' : 'Failed to load analytics'}
            </strong>
            <p className="text-xs text-red-300/80">{loadError.message}</p>
            <button
              onClick={loadData}
              className="mt-3 px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-white text-xs font-medium border border-white/10 transition-colors cursor-pointer"
            >
              Retry
            </button>
          </div>
        )}

        {!loading && !loadError && stats && stats.total_jobs === 0 && (
          <div className="p-12 text-center border border-dashed border-white/10 rounded-xl bg-white/[0.01]">
            <LayersIcon className="w-8 h-8 text-gray-500 mx-auto mb-2 opacity-60" />
            <p className="text-sm text-gray-400 font-medium">No projection jobs found</p>
            <p className="text-xs text-gray-500 mt-1">Activity over the last 30 days will appear here once new estimations are submitted.</p>
          </div>
        )}

        {stats && !loading && (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-8">
              <AnalyticsCard
                label="Total Estimations"
                value={stats.total_jobs}
                icon={<LayersIcon className="w-4 h-4 text-gray-300" />}
                color="text-white"
                helper="All bills analyzed"
              />
              <AnalyticsCard
                label="Successful"
                value={stats.done_jobs}
                icon={<CheckCircleIcon className="w-4 h-4 text-[#00C2BB]" />}
                color="text-[#00C2BB]"
                helper="Reports generated"
              />
              <AnalyticsCard
                label="Failed"
                value={stats.failed_jobs}
                icon={<AlertTriangleIcon className="w-4 h-4 text-orange-400" />}
                color="text-orange-400"
                helper="Validation or run errors"
              />
              <AnalyticsCard
                label="Failure Rate"
                value={`${Math.round(stats.failure_rate * 100)}%`}
                icon={<PercentIcon className="w-4 h-4 text-[#645DF6]" />}
                color="text-[#645DF6]"
                helper="Across all 30 days"
              />
            </div>

            <div className="grid gap-6 lg:grid-cols-3 mb-8">
              {/* Daily Volume Bar Chart */}
              <div className="lg:col-span-2 bg-white/[0.02] border border-white/10 rounded-xl p-5 shadow-sm">
                <div className="flex items-center justify-between mb-4">
                  <div className="flex items-center gap-2">
                    <ActivityIcon className="w-4 h-4 text-[#00C2BB]" />
                    <h3 className="text-xs font-semibold text-gray-200 uppercase tracking-wider">
                      Daily Projection Volume
                    </h3>
                  </div>
                  <span className="text-[11px] text-gray-500 font-mono">Last 30 Days</span>
                </div>
                <div style={{ width: '100%', height: 260 }}>
                  <ResponsiveContainer>
                    <BarChart data={chartData} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" vertical={false} />
                      <XAxis dataKey="day" stroke="rgba(255,255,255,0.4)" fontSize={11} tickLine={false} axisLine={{ stroke: 'rgba(255,255,255,0.1)' }} />
                      <YAxis stroke="rgba(255,255,255,0.4)" fontSize={11} allowDecimals={false} tickLine={false} axisLine={{ stroke: 'rgba(255,255,255,0.1)' }} />
                      <Tooltip
                        cursor={{ fill: 'rgba(255,255,255,0.04)' }}
                        contentStyle={{
                          background: '#0d0d14',
                          border: '1px solid rgba(255,255,255,0.15)',
                          borderRadius: '8px',
                          fontSize: '12px',
                          color: '#fff',
                        }}
                      />
                      <Bar dataKey="count" fill="#645DF6" radius={[4, 4, 0, 0]} maxBarSize={36} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>

              {/* Processing Time KPI */}
              <div className="bg-white/[0.02] border border-white/10 rounded-xl p-5 shadow-sm flex flex-col justify-between">
                <div>
                  <div className="flex items-center gap-2 mb-4">
                    <ClockIcon className="w-4 h-4 text-[#645DF6]" />
                    <h3 className="text-xs font-semibold text-gray-200 uppercase tracking-wider">
                      Avg. Processing Time
                    </h3>
                  </div>
                  <div className="mt-4">
                    <p className="text-3xl font-bold tabular-nums text-white">
                      {Math.round(stats.avg_processing_seconds / 60)} <span className="text-sm font-normal text-gray-400">minutes</span>
                    </p>
                    <p className="text-xs text-gray-500 mt-2 leading-relaxed">
                      Time from bill ingestion through SKU normalization, AI agent mapping, and 6-phase validation.
                    </p>
                  </div>
                </div>

                <div className="pt-4 border-t border-white/10 mt-6 flex items-center justify-between text-xs">
                  <span className="text-gray-400">Execution Mode</span>
                  <span className="font-mono text-[#00C2BB] bg-[#00C2BB]/10 px-2 py-0.5 rounded border border-[#00C2BB]/20">
                    6-Phase Pipeline
                  </span>
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </Layout>
  )
}

function AnalyticsCard({
  label,
  value,
  icon,
  color = 'text-white',
  helper,
}: Readonly<{
  label: string
  value: string | number
  icon: React.ReactNode
  color?: string
  helper?: string
}>) {
  return (
    <div className="bg-white/[0.02] border border-white/10 rounded-xl p-4 card-lift shadow-sm">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs uppercase tracking-wider text-gray-400 font-medium">{label}</span>
        <div className="w-7 h-7 rounded-lg bg-white/5 border border-white/10 flex items-center justify-center shrink-0">
          {icon}
        </div>
      </div>
      <p className={`text-2xl font-bold tabular-nums ${color}`}>{value}</p>
      {helper && <p className="text-[11px] text-gray-500 mt-1">{helper}</p>}
    </div>
  )
}

/* ── Standard Professional Vector Icons (Lucide-standard, no random graphics) ── */

function LayersIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
    </svg>
  )
}

function CheckCircleIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  )
}

function AlertTriangleIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
    </svg>
  )
}

function PercentIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M9 7a2 2 0 10-4 0 2 2 0 004 0zM19 17a2 2 0 10-4 0 2 2 0 004 0zM19 5L5 19" />
    </svg>
  )
}

function ActivityIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
    </svg>
  )
}

function ClockIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  )
}

function RefreshIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
    </svg>
  )
}
