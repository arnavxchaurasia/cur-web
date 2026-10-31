import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Layout } from '../components/Layout'
import { getAdminNotifications, type AdminNotificationsResponse } from '../api/admin'
import { getNotificationSettings, updateNotificationSettings } from '../api/auth'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'
import { toast } from 'sonner'
import { BackButton } from '../components/BackButton'

function statusBadge(status: string) {
  switch (status) {
    case 'done':
      return <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-[#00C2BB]/10 text-[#00C2BB] border border-[#00C2BB]/30">Completed</span>
    case 'failed':
      return <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-red-500/10 text-red-400 border border-red-500/30">Failed</span>
    case 'running':
      return <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-[#645DF6]/10 text-[#645DF6] border border-[#645DF6]/30 animate-pulse">Running</span>
    default:
      return <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-white/10 text-gray-400 border border-white/20">{status}</span>
  }
}

function ticketPriorityBadge(p: string) {
  switch (p) {
    case 'high':
      return <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-red-500/10 text-red-400 border border-red-500/30">High</span>
    case 'medium':
      return <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 text-amber-400 border border-amber-500/30">Medium</span>
    default:
      return <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-white/10 text-gray-400 border border-white/20">Low</span>
  }
}

export function AdminNotifications({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Admin Notifications — Facets')
  const [data, setData] = useState<AdminNotificationsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<'all' | 'jobs' | 'tickets'>('all')

  // Notification configuration state
  const [emailNotif, setEmailNotif] = useState(true)
  const [slackNotif, setSlackNotif] = useState(false)
  const [slackWebhook, setSlackWebhook] = useState('')
  const [slackChannel, setSlackChannel] = useState('')
  const [savingConfig, setSavingConfig] = useState(false)
  const [settingsSaved, setSettingsSaved] = useState(false)

  const loadSettings = async () => {
    try {
      const s = await getNotificationSettings()
      setEmailNotif(Boolean(s.email_notifications))
      setSlackNotif(Boolean(s.slack_notifications))
      setSlackWebhook(s.slack_webhook_url || '')
      setSlackChannel(s.slack_channel || '')
    } catch {
      // quiet fallback
    }
  }

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await getAdminNotifications()
      setData(res)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to load notifications')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    loadSettings()
  }, [])

  const handleToggleEmail = async () => {
    const nextVal = !emailNotif
    setEmailNotif(nextVal)
    setSavingConfig(true)
    try {
      await updateNotificationSettings({
        email_notifications: nextVal,
        slack_notifications: slackNotif,
        slack_webhook_url: slackWebhook,
        slack_channel: slackChannel,
      })
      setSettingsSaved(true)
      setTimeout(() => setSettingsSaved(false), 3000)
      toast.success(`Email reports are now ${nextVal ? 'enabled' : 'disabled'}`)
    } catch (err) {
      toast.error('Failed to update email preferences')
      setEmailNotif(!nextVal)
    } finally {
      setSavingConfig(false)
    }
  }

  const handleToggleSlack = async () => {
    const nextVal = !slackNotif
    setSlackNotif(nextVal)
    setSavingConfig(true)
    try {
      await updateNotificationSettings({
        email_notifications: emailNotif,
        slack_notifications: nextVal,
        slack_webhook_url: slackWebhook,
        slack_channel: slackChannel,
      })
      setSettingsSaved(true)
      setTimeout(() => setSettingsSaved(false), 3000)
      toast.success(`Slack notifications are now ${nextVal ? 'enabled' : 'disabled'}`)
    } catch (err) {
      toast.error('Failed to update Slack toggle')
      setSlackNotif(!nextVal)
    } finally {
      setSavingConfig(false)
    }
  }

  const handleSaveSlackConfig = async () => {
    setSavingConfig(true)
    try {
      await updateNotificationSettings({
        email_notifications: emailNotif,
        slack_notifications: slackNotif,
        slack_webhook_url: slackWebhook,
        slack_channel: slackChannel,
      })
      setSettingsSaved(true)
      setTimeout(() => setSettingsSaved(false), 3000)
      toast.success('Slack webhook and channel preferences saved')
    } catch (err) {
      toast.error('Failed to save Slack settings')
    } finally {
      setSavingConfig(false)
    }
  }

  const jobs = data?.my_jobs ?? []
  const tickets = data?.tickets ?? []

  const completedJobs = jobs.filter(j => j.status === 'done' || j.status === 'failed')
  const openTickets = tickets.filter(t => t.status === 'open' || t.status === 'in_progress')

  return (
    <Layout user={user}>
      <div className="max-w-5xl mx-auto px-6 py-10">
        <BackButton fallback="/admin" />
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-8">
          <div>
            <h1 className="text-2xl font-semibold mb-1">Admin Notifications</h1>
            <p className="text-sm text-gray-500">
              Live updates on estimation jobs initiated by you ({user.email}) and all customer support tickets.
            </p>
          </div>
          <button
            onClick={load}
            disabled={loading}
            className="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-lg border border-white/10 hover:border-white/25 bg-white/5 hover:bg-white/10 text-xs font-medium text-gray-200 transition-colors cursor-pointer disabled:opacity-50"
          >
            <RefreshIcon className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>

        {/* Quick KPI Counters */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-8">
          <div className="bg-white/[0.02] border border-white/10 rounded-xl p-4 card-lift">
            <span className="text-xs uppercase tracking-wider text-gray-400 font-medium">Your Estimations</span>
            <p className="text-2xl font-bold tabular-nums text-white mt-1">{jobs.length}</p>
            <p className="text-[11px] text-gray-500 mt-0.5">{completedJobs.length} completed / terminal</p>
          </div>
          <div className="bg-white/[0.02] border border-white/10 rounded-xl p-4 card-lift">
            <span className="text-xs uppercase tracking-wider text-gray-400 font-medium">Successful Runs</span>
            <p className="text-2xl font-bold tabular-nums text-[#00C2BB] mt-1">
              {jobs.filter(j => j.status === 'done').length}
            </p>
            <p className="text-[11px] text-gray-500 mt-0.5">Reports ready for hand-off</p>
          </div>
          <div className="bg-white/[0.02] border border-white/10 rounded-xl p-4 card-lift">
            <span className="text-xs uppercase tracking-wider text-gray-400 font-medium">Active Tickets</span>
            <p className="text-2xl font-bold tabular-nums text-[#645DF6] mt-1">{openTickets.length}</p>
            <p className="text-[11px] text-gray-500 mt-0.5">Open or in-progress tickets</p>
          </div>
          <div className="bg-white/[0.02] border border-white/10 rounded-xl p-4 card-lift">
            <span className="text-xs uppercase tracking-wider text-gray-400 font-medium">Total Tickets</span>
            <p className="text-2xl font-bold tabular-nums text-white mt-1">{tickets.length}</p>
            <p className="text-[11px] text-gray-500 mt-0.5">All customer inquiries</p>
          </div>
        </div>

        {/* Live Notification Configuration Panel */}
        <div className="bg-[#0e0e14] border border-white/15 rounded-2xl p-6 mb-8 shadow-xl">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-white/10">
            <div>
              <h2 className="text-sm font-semibold text-white tracking-wide uppercase flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-[#00C2BB]" />
                Notification Channels & Preferences
              </h2>
              <p className="text-xs text-gray-400 mt-0.5">
                Configure direct email alerts when reports are prepared and real Slack channel/DM webhook integrations.
              </p>
            </div>
            {settingsSaved && (
              <span className="text-xs text-[#00C2BB] bg-[#00C2BB]/10 border border-[#00C2BB]/30 px-2.5 py-1 rounded-full animate-fade-in">
                ✓ Preferences Saved
              </span>
            )}
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mt-6">
            {/* 1. Email Report Alert Toggle */}
            <div className="p-4 rounded-xl bg-white/[0.02] border border-white/10 flex flex-col justify-between space-y-4">
              <div>
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="w-7 h-7 rounded-lg bg-[#00C2BB]/10 border border-[#00C2BB]/30 text-[#00C2BB] flex items-center justify-center">
                      <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
                      </svg>
                    </span>
                    <span className="text-sm font-medium text-white">Email Report Dispatch</span>
                  </div>

                  <button
                    type="button"
                    role="switch"
                    aria-checked={emailNotif}
                    onClick={() => handleToggleEmail()}
                    disabled={savingConfig}
                    className={`relative inline-flex items-center h-6 w-11 shrink-0 rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out cursor-pointer focus:outline-none focus:ring-2 focus:ring-[#00C2BB]/40 focus:ring-offset-2 focus:ring-offset-[#0e0e14] disabled:opacity-50 ${
                      emailNotif ? 'bg-[#00C2BB]' : 'bg-white/20 hover:bg-white/30'
                    }`}
                  >
                    <span
                      aria-hidden="true"
                      className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-md ring-0 transition duration-200 ease-in-out ${
                        emailNotif ? 'translate-x-5' : 'translate-x-0.5'
                      }`}
                    />
                  </button>
                </div>
                <p className="text-xs text-gray-400 mt-2 leading-relaxed">
                  Automatically send an email to <span className="text-gray-200 font-mono">{user.email}</span> when your bill analysis and report have been generated and prepared.
                </p>
              </div>

              <div className="text-[11px] text-gray-500 font-mono">
                Status: {emailNotif ? <strong className="text-[#00C2BB]">Active (Sending)</strong> : 'Disabled'}
              </div>
            </div>

            {/* 2. Slack Webhook & Channel/DM Configuration */}
            <div className="p-4 rounded-xl bg-white/[0.02] border border-white/10 flex flex-col justify-between space-y-4">
              <div>
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <span className="w-7 h-7 rounded-lg bg-[#645DF6]/10 border border-[#645DF6]/30 text-[#645DF6] flex items-center justify-center">
                      <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z" />
                      </svg>
                    </span>
                    <span className="text-sm font-medium text-white">Slack Webhook & Channel</span>
                  </div>

                  <button
                    type="button"
                    role="switch"
                    aria-checked={slackNotif}
                    onClick={() => handleToggleSlack()}
                    disabled={savingConfig}
                    className={`relative inline-flex items-center h-6 w-11 shrink-0 rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out cursor-pointer focus:outline-none focus:ring-2 focus:ring-[#00C2BB]/40 focus:ring-offset-2 focus:ring-offset-[#0e0e14] disabled:opacity-50 ${
                      slackNotif ? 'bg-[#00C2BB]' : 'bg-white/20 hover:bg-white/30'
                    }`}
                  >
                    <span
                      aria-hidden="true"
                      className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-md ring-0 transition duration-200 ease-in-out ${
                        slackNotif ? 'translate-x-5' : 'translate-x-0.5'
                      }`}
                    />
                  </button>
                </div>

                <div className="space-y-3">
                  <div>
                    <label className="block text-[11px] text-gray-400 mb-1">Incoming Webhook URL</label>
                    <input
                      type="url"
                      placeholder="https://hooks.slack.com/services/..."
                      value={slackWebhook}
                      onChange={e => setSlackWebhook(e.target.value)}
                      className="w-full px-3 py-1.5 bg-[#090a0f] border border-white/15 rounded-lg text-xs text-gray-200 placeholder-gray-600 focus:outline-none focus:border-[#00C2BB]"
                    />
                  </div>

                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <label className="text-[11px] text-gray-400">Target Channel or DM</label>
                      {slackChannel && (
                        <span className="text-[10px] font-mono text-[#00C2BB] bg-[#00C2BB]/10 border border-[#00C2BB]/30 px-2 py-0.5 rounded-full">
                          Activated: {slackChannel.startsWith('#') || slackChannel.startsWith('@') ? slackChannel : '#' + slackChannel}
                        </span>
                      )}
                    </div>
                    <input
                      type="text"
                      placeholder="#cost-alerts or @username"
                      value={slackChannel}
                      onChange={e => setSlackChannel(e.target.value)}
                      className="w-full px-3 py-1.5 bg-[#090a0f] border border-white/15 rounded-lg text-xs text-gray-200 placeholder-gray-600 focus:outline-none focus:border-[#00C2BB]"
                    />
                    <p className="text-[10px] text-gray-500 mt-1">
                      Target any Slack channel (e.g. <span className="text-gray-400 font-mono">#finops</span>, <span className="text-gray-400 font-mono">#aws-reports</span>) or personal direct message (<span className="text-gray-400 font-mono">@username</span>) routed through your webhook.
                    </p>
                  </div>
                </div>
              </div>

              <div className="flex items-center justify-between pt-2 border-t border-white/5">
                <div className="flex items-center gap-2">
                  <span className={`w-2 h-2 rounded-full ${slackNotif && slackWebhook ? 'bg-[#00C2BB]' : 'bg-gray-600'}`} />
                  <span className="text-[11px] text-gray-400">
                    {slackNotif && slackWebhook
                      ? `Active · Routing to ${slackChannel || 'Default channel'}`
                      : 'Not sending (Enter webhook & enable)'}
                  </span>
                </div>
                <button
                  onClick={handleSaveSlackConfig}
                  disabled={savingConfig}
                  className="px-3.5 py-1.5 bg-[#645DF6] hover:bg-[#645DF6]/90 text-white rounded-lg text-xs font-semibold transition cursor-pointer disabled:opacity-50"
                >
                  {savingConfig ? 'Saving…' : 'Save Slack Settings'}
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* Filter Tabs */}
        <div className="flex items-center gap-2 border-b border-white/10 pb-3 mb-6">
          <button
            onClick={() => setActiveTab('all')}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer ${
              activeTab === 'all' ? 'bg-white/10 text-white' : 'text-gray-400 hover:text-white'
            }`}
          >
            All Activity
          </button>
          <button
            onClick={() => setActiveTab('jobs')}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer ${
              activeTab === 'jobs' ? 'bg-white/10 text-white' : 'text-gray-400 hover:text-white'
            }`}
          >
            My Job Runs ({jobs.length})
          </button>
          <button
            onClick={() => setActiveTab('tickets')}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer ${
              activeTab === 'tickets' ? 'bg-white/10 text-white' : 'text-gray-400 hover:text-white'
            }`}
          >
            Raised Tickets ({tickets.length})
          </button>
        </div>

        {loading && (
          <div className="flex flex-col items-center justify-center py-20 text-gray-500 gap-3">
            <div className="w-6 h-6 border-2 border-[#00C2BB] border-t-transparent rounded-full animate-spin" />
            <p className="text-xs">Fetching notification feed…</p>
          </div>
        )}

        {error && (
          <div className="p-4 rounded-xl bg-red-950/30 border border-red-500/30 text-red-400 text-sm mb-6">
            <strong className="block font-semibold mb-1">Error Loading Notifications</strong>
            <p className="text-xs text-red-300/80">{error}</p>
          </div>
        )}

        {!loading && !error && (
          <div className="space-y-8">
            {/* Section 1: My Completed & Active Jobs */}
            {(activeTab === 'all' || activeTab === 'jobs') && (
              <div className="bg-white/[0.02] border border-white/10 rounded-xl overflow-hidden shadow-sm">
                <div className="px-6 py-4 border-b border-white/10 bg-white/[0.01] flex items-center justify-between">
                  <div className="flex items-center gap-2.5">
                    <CheckCircleIcon className="w-4 h-4 text-[#00C2BB]" />
                    <div>
                      <h2 className="text-xs font-bold text-gray-200 uppercase tracking-wider">
                        My Cost Projection Runs
                      </h2>
                      <p className="text-[11px] text-gray-500 mt-0.5">
                        Direct pipeline status for bills submitted under {user.email}
                      </p>
                    </div>
                  </div>
                  <span className="text-[11px] font-mono text-gray-500">{jobs.length} jobs</span>
                </div>

                {jobs.length === 0 ? (
                  <div className="p-8 text-center text-xs text-gray-500">
                    No jobs submitted under your email yet.
                  </div>
                ) : (
                  <div className="divide-y divide-white/5">
                    {jobs.map(j => (
                      <div key={j.id} className="p-4 sm:px-6 hover:bg-white/[0.015] transition-colors flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                        <div className="space-y-1">
                          <div className="flex items-center gap-2">
                            <span className="font-semibold text-white text-sm">{j.prospect}</span>
                            {statusBadge(j.status)}
                          </div>
                          <div className="flex items-center gap-3 text-xs text-gray-400">
                            <span className="font-mono text-[11px] text-gray-500">ID: {j.id.slice(0, 8)}</span>
                            <span>•</span>
                            <span>{new Date(j.created_at).toLocaleString()}</span>
                            {j.aws_spend != null && (
                              <>
                                <span>•</span>
                                <span className="font-mono text-gray-300">AWS: ${j.aws_spend.toLocaleString(undefined, { maximumFractionDigits: 0 })}</span>
                              </>
                            )}
                          </div>
                          {j.error && (
                            <p className="text-xs text-red-400 font-mono mt-1">{j.error}</p>
                          )}
                        </div>

                        <div className="flex items-center gap-2 shrink-0">
                          <Link
                            to={`/jobs/${j.id}`}
                            className="px-3 py-1.5 rounded-lg border border-white/10 hover:border-white/30 text-xs font-medium text-gray-200 hover:text-white transition-colors"
                          >
                            View Job Status →
                          </Link>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Section 2: Raised Support Tickets */}
            {(activeTab === 'all' || activeTab === 'tickets') && (
              <div className="bg-white/[0.02] border border-white/10 rounded-xl overflow-hidden shadow-sm">
                <div className="px-6 py-4 border-b border-white/10 bg-white/[0.01] flex items-center justify-between">
                  <div className="flex items-center gap-2.5">
                    <TicketIcon className="w-4 h-4 text-[#645DF6]" />
                    <div>
                      <h2 className="text-xs font-bold text-gray-200 uppercase tracking-wider">
                        Raised Support Tickets
                      </h2>
                      <p className="text-[11px] text-gray-500 mt-0.5">
                        Customer inquiries, billing questions, and technical support requests
                      </p>
                    </div>
                  </div>
                  <Link
                    to="/admin/tickets"
                    className="text-[11px] text-[#00C2BB] hover:underline"
                  >
                    Manage Tickets →
                  </Link>
                </div>

                {tickets.length === 0 ? (
                  <div className="p-8 text-center text-xs text-gray-500">
                    No tickets raised in the system.
                  </div>
                ) : (
                  <div className="divide-y divide-white/5">
                    {tickets.map(t => (
                      <div key={t.id} className="p-4 sm:px-6 hover:bg-white/[0.015] transition-colors flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                        <div className="space-y-1">
                          <div className="flex items-center gap-2">
                            <span className="font-semibold text-white text-sm">{t.title}</span>
                            <span className="px-2 py-0.5 rounded text-[10px] font-bold border border-current capitalize text-gray-300">
                              {t.status}
                            </span>
                            {ticketPriorityBadge(t.priority)}
                          </div>
                          <p className="text-xs text-gray-400 line-clamp-2 max-w-2xl leading-relaxed">
                            {t.description}
                          </p>
                          <div className="flex items-center gap-3 text-[11px] text-gray-500 pt-0.5">
                            <span className="font-mono">{t.id}</span>
                            <span>•</span>
                            <span>Raised by: <strong className="text-gray-400">{t.created_by}</strong></span>
                            <span>•</span>
                            <span className="capitalize">{t.category}</span>
                            <span>•</span>
                            <span>{new Date(t.created_at).toLocaleString()}</span>
                          </div>
                        </div>

                        <div className="flex items-center gap-2 shrink-0">
                          <Link
                            to="/admin/tickets"
                            className="px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 border border-white/10 text-xs font-medium text-gray-200 hover:text-white transition-colors"
                          >
                            Open Ticket →
                          </Link>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </Layout>
  )
}

/* ── Standard Professional Vector Icons ── */

function CheckCircleIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  )
}

function TicketIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M15 5v2m0 4v2m0 4v2M5 5a2 2 0 00-2 2v3a2 2 0 110 4v3a2 2 0 002 2h14a2 2 0 002-2v-3a2 2 0 110-4V7a2 2 0 00-2-2H5z" />
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
