import { useEffect, useState } from 'react'
import { Layout } from '../components/Layout'
import {
  getIntegrations,
  updateIntegrations,
  testNotify,
  type IntegrationsConfig,
} from '../api/admin'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'
import { BackButton } from '../components/BackButton'

type TestState = { status: 'idle' | 'sending' | 'ok' | 'error'; msg?: string }

export function AdminIntegrations({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Integrations — admin')
  const [cfg, setCfg] = useState<IntegrationsConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedMsg, setSavedMsg] = useState<string | null>(null)

  // Draft fields. Secrets are write-only: blank means "keep current".
  const [emailEnabled, setEmailEnabled] = useState(true)
  const [emailFrom, setEmailFrom] = useState('')
  const [emailKey, setEmailKey] = useState('')
  const [slackEnabled, setSlackEnabled] = useState(true)
  const [slackWebhook, setSlackWebhook] = useState('')

  const [emailTest, setEmailTest] = useState<TestState>({ status: 'idle' })
  const [slackTest, setSlackTest] = useState<TestState>({ status: 'idle' })

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const d = await getIntegrations()
      setCfg(d)
      setEmailEnabled(d.email.enabled)
      setEmailFrom(d.email.from)
      setSlackEnabled(d.slack.enabled)
      setEmailKey('')
      setSlackWebhook('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to load integrations')
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { refresh() }, [])

  const save = async () => {
    setSaving(true)
    setSavedMsg(null)
    setError(null)
    try {
      const d = await updateIntegrations({
        email: { enabled: emailEnabled, from: emailFrom, ...(emailKey ? { api_key: emailKey } : {}) },
        slack: { enabled: slackEnabled, ...(slackWebhook ? { webhook_url: slackWebhook } : {}) },
      })
      setCfg(d)
      setEmailKey('')
      setSlackWebhook('')
      setSavedMsg('Saved.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'save failed')
    } finally {
      setSaving(false)
    }
  }

  const runTest = async (channel: 'email' | 'slack') => {
    const set = channel === 'email' ? setEmailTest : setSlackTest
    set({ status: 'sending' })
    try {
      const r = await testNotify(channel)
      if (r.status === 'sent') set({ status: 'ok', msg: `Test ${channel} sent to ${user.email}` })
      else set({ status: 'error', msg: r.error || 'failed' })
    } catch (err) {
      set({ status: 'error', msg: err instanceof Error ? err.message : 'failed' })
    }
  }

  const secretLabel = (s: { configured: boolean; last4?: string }, envFallback: boolean) => {
    if (s.configured) return `Configured ✓ (…${s.last4 ?? ''})`
    if (envFallback) return 'Set via environment (enter a value here to override)'
    return 'Not set'
  }

  return (
    <Layout user={user}>
      <div className="max-w-3xl mx-auto px-4 py-6">
        <BackButton />
        <h1 className="text-2xl font-semibold text-white mb-1">Notification Integrations</h1>
        <p className="text-sm text-gray-400 mb-6">
          Configure how report-ready and job notifications are delivered. Secrets are encrypted at rest and never shown again.
        </p>

        {loading && <p className="text-gray-400">Loading…</p>}
        {error && <div className="mb-4 rounded bg-red-900/40 border border-red-700 text-red-200 px-4 py-2 text-sm">{error}</div>}

        {cfg && !cfg.encryption_available && (
          <div className="mb-6 rounded bg-amber-900/40 border border-amber-700 text-amber-200 px-4 py-3 text-sm">
            <strong>SETTINGS_ENCRYPTION_KEY is not set on the server.</strong> You can toggle channels and edit the
            From address, but API keys and webhook URLs cannot be saved until it is configured (base64 of 32 random bytes).
          </div>
        )}

        {cfg && (
          <>
            {/* Email card */}
            <section className="mb-6 rounded-lg border border-gray-700 bg-gray-800/50 p-5">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-medium text-white">Email (Resend)</h2>
                <label className="flex items-center gap-2 text-sm text-gray-300">
                  <input type="checkbox" checked={emailEnabled} onChange={e => setEmailEnabled(e.target.checked)} />
                  Enabled
                </label>
              </div>
              <label className="block text-sm text-gray-400 mb-1">From address</label>
              <input className="w-full mb-3 rounded bg-gray-900 border border-gray-600 px-3 py-2 text-white text-sm"
                value={emailFrom} onChange={e => setEmailFrom(e.target.value)}
                placeholder="Facets Cloud <no-reply@facets.cloud>" />
              <label className="block text-sm text-gray-400 mb-1">Resend API key</label>
              <input type="password" autoComplete="off"
                className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-white text-sm"
                value={emailKey} onChange={e => setEmailKey(e.target.value)}
                placeholder="Leave blank to keep current" />
              <p className="mt-1 text-xs text-gray-500">{secretLabel(cfg.email.api_key, cfg.email.env_fallback)}</p>
              <div className="mt-4 flex items-center gap-3">
                <button onClick={() => runTest('email')} disabled={emailTest.status === 'sending'}
                  className="rounded border border-gray-600 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700 disabled:opacity-50">
                  {emailTest.status === 'sending' ? 'Sending…' : 'Send test email'}
                </button>
                {emailTest.status === 'ok' && <span className="text-sm text-green-400">{emailTest.msg}</span>}
                {emailTest.status === 'error' && <span className="text-sm text-red-400">{emailTest.msg}</span>}
              </div>
            </section>

            {/* Slack card */}
            <section className="mb-6 rounded-lg border border-gray-700 bg-gray-800/50 p-5">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-medium text-white">Slack</h2>
                <label className="flex items-center gap-2 text-sm text-gray-300">
                  <input type="checkbox" checked={slackEnabled} onChange={e => setSlackEnabled(e.target.checked)} />
                  Enabled
                </label>
              </div>
              <label className="block text-sm text-gray-400 mb-1">Incoming webhook URL</label>
              <input type="password" autoComplete="off"
                className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-white text-sm"
                value={slackWebhook} onChange={e => setSlackWebhook(e.target.value)}
                placeholder="Leave blank to keep current" />
              <p className="mt-1 text-xs text-gray-500">{secretLabel(cfg.slack.webhook_url, cfg.slack.env_fallback)}</p>
              <p className="mt-1 text-xs text-gray-500">
                The webhook determines the target channel — create one per channel in Slack and paste its URL here.
              </p>
              <div className="mt-4 flex items-center gap-3">
                <button onClick={() => runTest('slack')} disabled={slackTest.status === 'sending'}
                  className="rounded border border-gray-600 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700 disabled:opacity-50">
                  {slackTest.status === 'sending' ? 'Sending…' : 'Send test message'}
                </button>
                {slackTest.status === 'ok' && <span className="text-sm text-green-400">{slackTest.msg}</span>}
                {slackTest.status === 'error' && <span className="text-sm text-red-400">{slackTest.msg}</span>}
              </div>
            </section>

            <div className="flex items-center gap-3">
              <button onClick={save} disabled={saving}
                className="rounded bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50">
                {saving ? 'Saving…' : 'Save changes'}
              </button>
              {savedMsg && <span className="text-sm text-green-400">{savedMsg}</span>}
            </div>
          </>
        )}
      </div>
    </Layout>
  )
}
