import { useEffect, useState } from 'react'
import { Layout } from '../components/Layout'
import {
  getReportSettings,
  putReportSetting,
  getReportSettingHistory,
  resetReportSettings,
  type ReportSetting,
  type ReportSettingChange,
  type ReportSettingsResponse,
} from '../api/admin'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { BackButton } from '../components/BackButton'

export function AdminReportSettings({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Report settings — admin')
  const [data, setData] = useState<ReportSettingsResponse | null>(null)
  const [history, setHistory] = useState<ReportSettingChange[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState<string | null>(null) // key being saved
  const [editingValue, setEditingValue] = useState<string>('')
  const [resetConfirmOpen, setResetConfirmOpen] = useState(false)
  const [resetting, setResetting] = useState(false)

  const groups = ['sections', 'columns', 'rows'] as const

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const d = await getReportSettings()
      setData(d)
      const h = await getReportSettingHistory()
      setHistory(h)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to load report settings')
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { refresh() }, [])

  const handleSave = async (setting: ReportSetting, value: string) => {
    // Validate numeric
    if (setting.value_type === 'number') {
      const num = Number(value)
      if (!Number.isFinite(num)) {
        setError('value must be a number')
        return
      }
      if (setting.min_value != null && num < setting.min_value) {
        setError(`value must be >= ${setting.min_value}`)
        return
      }
      if (setting.max_value != null && num > setting.max_value) {
        setError(`value must be <= ${setting.max_value}`)
        return
      }
    } else if (setting.value_type === 'bool') {
      const lower = value.trim().toLowerCase()
      if (lower !== 'true' && lower !== 'false' && lower !== '1' && lower !== '0' && lower !== 'yes' && lower !== 'no') {
        setError('value must be true or false')
        return
      }
    }

    let reason = editingValue.trim()
    if (!reason) {
      if (setting.value_type === 'bool') {
        reason = `Toggled ${setting.key} to ${value}`
      } else {
        setError('a reason is required to change a setting')
        return
      }
    }

    setSaving(setting.key)
    setError(null)
    try {
      // Normalize bool to true/false
      let normalized = value
      if (setting.value_type === 'bool') {
        const lower = value.trim().toLowerCase()
        normalized = lower === 'true' || lower === '1' || lower === 'yes' ? 'true' : 'false'
      }
      const result = await putReportSetting(setting.key, { value: normalized, reason })
      if (result) {
        setEditingValue('')
        await refresh()
      } else {
        setError('save failed')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'save failed')
    } finally {
      setSaving(null)
    }
  }

  const handleReset = async () => {
    setResetting(true)
    setError(null)
    try {
      const result = await resetReportSettings()
      if (result) {
        await refresh()
        setResetConfirmOpen(false)
      } else {
        setError('reset failed')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'reset failed')
    } finally {
      setResetting(false)
    }
  }

  if (loading) {
    return (
      <Layout user={user}>
        <div className="max-w-2xl mx-auto px-6 py-10">
          <p className="text-sm text-gray-500">Loading…</p>
        </div>
      </Layout>
    )
  }

  if (error) {
    return (
      <Layout user={user}>
        <div className="max-w-2xl mx-auto px-6 py-10">
          <p className="text-xs text-orange-400">{error}</p>
        </div>
      </Layout>
    )
  }

  return (
    <Layout user={user}>
      <div className="max-w-5xl mx-auto px-6 py-10">
        <BackButton fallback="/admin" />
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-8">
          <div>
            <h1 className="text-2xl font-semibold mb-1">Report Settings</h1>
            <p className="text-sm text-gray-500 max-w-2xl">
              Configure sections, columns, and line-item filters included in customer-facing cost projection reports.
            </p>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={refresh}
              disabled={loading}
              className="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-lg border border-white/10 hover:border-white/25 bg-white/5 hover:bg-white/10 text-xs font-medium text-gray-200 transition-colors cursor-pointer disabled:opacity-50"
            >
              <RefreshIcon className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
              Refresh
            </button>
          </div>
        </div>

        {data && (
          <div className="space-y-8">
            {/* Version & Info Card */}
            <div className="bg-white/[0.02] border border-white/10 rounded-xl p-5 shadow-sm flex flex-col sm:flex-row sm:items-center justify-between gap-4">
              <div className="flex items-start gap-3">
                <div className="w-8 h-8 rounded-lg bg-[#00C2BB]/10 border border-[#00C2BB]/20 flex items-center justify-center shrink-0 mt-0.5">
                  <ShieldCheckIcon className="w-4 h-4 text-[#00C2BB]" />
                </div>
                <div>
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-300">Active Settings Configuration</h3>
                  <p className="text-xs text-gray-500 mt-1 leading-relaxed">
                    Settings apply instantly to all subsequent report generations. Totals are preserved across all line items.
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0 self-start sm:self-auto">
                <span className="text-xs text-gray-400">Fingerprint:</span>
                <span className="font-mono text-xs font-semibold text-[#00C2BB] bg-[#00C2BB]/10 px-2.5 py-1 rounded border border-[#00C2BB]/20">
                  {data.version.slice(0, 16)}…
                </span>
              </div>
            </div>

            {/* Settings Sections */}
            {groups.map((group) => {
              const settingsInGroup = data.settings.filter((s) => s.group_name === group)
              const groupLabel = group === 'sections' ? 'Report Sections' : group === 'columns' ? 'Data Columns' : 'Row Visibility Filters'
              const groupDesc = group === 'sections'
                ? 'Control top-level summaries, breakdowns, and appendix visibility'
                : group === 'columns'
                ? 'Select pricing, discount, and unit metric columns shown in tables'
                : 'Define filtering thresholds and exclusion rules for micro-charges'

              return (
                <div key={group} className="bg-white/[0.02] border border-white/10 rounded-xl overflow-hidden shadow-sm">
                  <div className="px-6 py-4 border-b border-white/10 bg-white/[0.01] flex items-center justify-between">
                    <div className="flex items-center gap-2.5">
                      {group === 'sections' && <LayoutIcon className="w-4 h-4 text-[#645DF6]" />}
                      {group === 'columns' && <ColumnsIcon className="w-4 h-4 text-[#00C2BB]" />}
                      {group === 'rows' && <FilterIcon className="w-4 h-4 text-amber-400" />}
                      <div>
                        <h2 className="text-xs font-bold text-gray-200 uppercase tracking-wider">
                          {groupLabel}
                        </h2>
                        <p className="text-[11px] text-gray-500 mt-0.5">{groupDesc}</p>
                      </div>
                    </div>
                    <span className="text-[11px] font-mono text-gray-500">
                      {settingsInGroup.length} items
                    </span>
                  </div>

                  <table className="w-full text-left text-xs border-collapse">
                    <thead>
                      <tr className="border-b border-white/10 bg-white/[0.01]">
                        <th className="py-3 px-6 text-gray-400 font-medium w-1/2">Setting &amp; Description</th>
                        <th className="py-3 px-4 text-gray-400 font-medium w-1/4">Status / Value</th>
                        <th className="py-3 px-6 text-gray-400 font-medium text-right">Actions</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-white/5">
                      {settingsInGroup.map((s) => {
                        const isBool = s.value_type === 'bool'
                        const isChecked = s.value === 'true'
                        return (
                          <tr key={s.key} className="hover:bg-white/[0.02] transition-colors">
                            <td className="py-3.5 px-6">
                              <div className="font-semibold text-white text-xs">{s.title}</div>
                              <div className="text-[10px] font-mono text-gray-500 mt-0.5">{s.key}</div>
                              <div className="text-[11px] text-gray-400 mt-1 leading-relaxed">{s.description}</div>
                            </td>
                            <td className="py-3.5 px-4">
                              {isBool ? (
                                <button
                                  type="button"
                                  onClick={() => handleSave(s, isChecked ? 'false' : 'true')}
                                  className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
                                    isChecked ? 'bg-[#00C2BB]' : 'bg-white/20'
                                  }`}
                                  role="switch"
                                  aria-checked={isChecked}
                                >
                                  <span
                                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                                      isChecked ? 'translate-x-4' : 'translate-x-0'
                                    }`}
                                  />
                                </button>
                              ) : (
                                <div className="space-y-1">
                                  <input
                                    type="text"
                                    value={editingValue === s.key ? editingValue : s.value}
                                    onChange={(e) => setEditingValue(e.target.value)}
                                    onBlur={() => {
                                      if (editingValue === s.key) {
                                        handleSave(s, editingValue)
                                        setEditingValue('')
                                      }
                                    }}
                                    className="bg-white/5 border border-white/10 rounded px-2.5 py-1 text-white text-xs w-28 focus:border-[#645DF6] outline-none"
                                  />
                                  <div className="text-[10px] text-gray-500 font-mono">
                                    Range: [{s.min_value ?? '–'} to {s.max_value ?? '–'}]
                                  </div>
                                </div>
                              )}
                            </td>
                            <td className="py-3.5 px-6 text-right">
                              {saving === s.key ? (
                                <span className="text-[11px] text-[#00C2BB] font-medium animate-pulse inline-flex items-center gap-1.5">
                                  <RefreshIcon className="w-3 h-3 animate-spin" /> Saving…
                                </span>
                              ) : !isBool ? (
                                <div className="flex items-center justify-end gap-2">
                                  <button
                                    onClick={() => setEditingValue(s.value)}
                                    className="px-2.5 py-1 rounded border border-white/10 hover:border-white/30 text-gray-300 hover:text-white text-xs transition-colors cursor-pointer"
                                  >
                                    Edit
                                  </button>
                                  {editingValue === s.key && (
                                    <button
                                      onClick={() => {
                                        handleSave(s, editingValue)
                                        setEditingValue('')
                                      }}
                                      className="px-2.5 py-1 rounded bg-[#00C2BB] text-black font-semibold text-xs hover:bg-[#00C2BB]/90 transition-colors cursor-pointer"
                                    >
                                      Save
                                    </button>
                                  )}
                                </div>
                              ) : (
                                <span className={`text-[11px] font-medium ${isChecked ? 'text-[#00C2BB]' : 'text-gray-500'}`}>
                                  {isChecked ? 'Enabled' : 'Disabled'}
                                </span>
                              )}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )
            })}

            {/* Change History */}
            <div className="bg-white/[0.02] border border-white/10 rounded-xl p-6 shadow-sm">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                  <HistoryIcon className="w-4 h-4 text-gray-400" />
                  <h2 className="text-xs font-semibold text-gray-300 uppercase tracking-wider">
                    Change Audit Log
                  </h2>
                </div>
                <span className="text-[11px] text-gray-500">{history.length} records</span>
              </div>

              {history.length === 0 ? (
                <p className="text-xs text-gray-500 py-4 text-center">No setting changes recorded yet.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs border-collapse">
                    <thead>
                      <tr className="border-b border-white/10 text-gray-400">
                        <th className="pb-3 font-medium">Timestamp</th>
                        <th className="pb-3 font-medium">Setting Key</th>
                        <th className="pb-3 font-medium">Transition</th>
                        <th className="pb-3 font-medium text-right">Modified By</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-white/5">
                      {history.map((h) => (
                        <tr key={h.id} className="hover:bg-white/[0.01]">
                          <td className="py-2.5 text-gray-400 font-mono text-[11px]">{h.changed_at}</td>
                          <td className="py-2.5 font-mono text-gray-300">{h.key}</td>
                          <td className="py-2.5">
                            <span className="text-gray-500">{h.old_value}</span>
                            <span className="text-gray-600 mx-2">→</span>
                            <span className="text-[#00C2BB] font-medium">{h.new_value}</span>
                          </td>
                          <td className="py-2.5 text-gray-400 text-right">{h.changed_by}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {/* Danger Zone: Reset to Defaults */}
            <div className="bg-red-950/10 border border-red-500/20 rounded-xl p-6 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
              <div>
                <h3 className="text-xs font-semibold uppercase tracking-wider text-red-400">Reset All Settings</h3>
                <p className="text-xs text-gray-400 mt-1">Revert all report configuration switches and parameters back to system defaults.</p>
              </div>
              <button
                onClick={() => setResetConfirmOpen(true)}
                className="px-4 py-2 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 hover:bg-red-500/20 hover:text-red-300 text-xs font-medium transition-colors cursor-pointer shrink-0 self-start sm:self-auto"
              >
                Reset to Defaults
              </button>
            </div>

            <ConfirmDialog
              open={resetConfirmOpen}
              onOpenChange={setResetConfirmOpen}
              title="Reset report settings"
              description="Reset all report settings to their defaults? This cannot be undone."
              confirmLabel="Reset settings"
              destructive
              loading={resetting}
              onConfirm={handleReset}
            />
          </div>
        )}
      </div>
    </Layout>
  )
}

/* ── Standard Professional Icons ── */

function ShieldCheckIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
    </svg>
  )
}

function LayoutIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M4 5a1 1 0 011-1h14a1 1 0 011 1v2a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM4 13a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H5a1 1 0 01-1-1v-6zM16 13a1 1 0 011-1h2a1 1 0 011 1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-6z" />
    </svg>
  )
}

function ColumnsIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M9 4v16m6-16v16M4 4h16a1 1 0 011 1v14a1 1 0 01-1 1H4a1 1 0 01-1-1V5a1 1 0 011-1z" />
    </svg>
  )
}

function FilterIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M3 4a1 1 0 011-1h16a1 1 0 011 1v2.586a1 1 0 01-.293.707l-6.414 6.414a1 1 0 00-.293.707V17l-4 4v-6.586a1 1 0 00-.293-.707L3.293 7.293A1 1 0 013 6.586V4z" />
    </svg>
  )
}

function HistoryIcon({ className = 'w-4 h-4' }: Readonly<{ className?: string }>) {
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