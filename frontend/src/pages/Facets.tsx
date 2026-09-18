import { useEffect, useMemo, useRef, useState } from 'react'
import { Nav } from '../components/Nav'
import { Reveal } from '../components/Reveal'
import { facetsListJobs, facetsGetLogs, facetsGeneratePortfolioSummary, facetsPortfolioSummaryHTMLURL } from '../api/facets'
import type { PortfolioSummary } from '../api/facets'
import type { Job } from '../api/jobs'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'

function fmtMoney(v: number) {
  return v.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 })
}
function fmtPct(v: number) {
  return `${(v * 100).toFixed(1)}%`
}

function LogsPanel({ jobs }: Readonly<{ jobs: Job[] }>) {
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<string>('')
  const [lines, setLines] = useState<string[] | null>(null)
  const [loading, setLoading] = useState(false)

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return jobs
    return jobs.filter(j => j.prospect.toLowerCase().includes(q) || j.owner.toLowerCase().includes(q))
  }, [jobs, query])

  useEffect(() => {
    if (!selected) { setLines(null); return }
    setLoading(true)
    facetsGetLogs(selected).then(l => { setLines(l); setLoading(false) })
  }, [selected])

  return (
    <div className="space-y-4">
      <input
        type="text"
        value={query}
        onChange={e => setQuery(e.target.value)}
        placeholder="Filter by prospect or owner…"
        className="w-full sm:w-96 bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm
          placeholder:text-gray-500 transition-colors duration-150 focus:border-[#645DF6]"
      />
      <select
        value={selected}
        onChange={e => setSelected(e.target.value)}
        className="bg-[#12121a] text-white border border-white/10 rounded-lg px-3 py-2 text-sm w-full sm:w-96"
      >
        <option className="bg-[#12121a] text-white" value="">
          Select a job… ({filtered.length} match{filtered.length === 1 ? '' : 'es'})
        </option>
        {filtered.map(j => (
          <option className="bg-[#12121a] text-white" key={j.id} value={j.id}>
            {j.prospect} — {j.id.slice(0, 8)} ({j.status})
          </option>
        ))}
      </select>

      {loading && <p className="text-sm text-gray-500">Loading…</p>}
      {lines && lines.length === 0 && !loading && (
        <p className="text-sm text-gray-500">No log output yet for this job.</p>
      )}
      {lines && lines.length > 0 && (
        <pre className="bg-black/40 border border-white/10 rounded-lg p-4 text-xs text-gray-300
          overflow-auto max-h-[60vh] whitespace-pre-wrap leading-relaxed">
          {lines.join('\n')}
        </pre>
      )}
    </div>
  )
}

function CategoryTable({ rows, showShare }: Readonly<{ rows: PortfolioSummary['by_category']; showShare?: boolean }>) {
  return (
    <table className="w-full text-sm">
      <thead className="bg-white/[0.03] text-xs uppercase tracking-wider text-gray-400">
        <tr>
          <th className="text-left px-3 py-2">Category</th>
          <th className="text-right px-3 py-2">Items</th>
          <th className="text-right px-3 py-2">AWS</th>
          <th className="text-right px-3 py-2">GCP OD</th>
          <th className="text-right px-3 py-2">Diff</th>
          <th className="text-right px-3 py-2">Diff %</th>
          {showShare && <th className="text-right px-3 py-2">Share</th>}
        </tr>
      </thead>
      <tbody className="divide-y divide-white/5">
        {rows.map(r => (
          <tr key={r.category}>
            <td className="px-3 py-2 text-gray-200">{r.category}</td>
            <td className="px-3 py-2 text-right tabular-nums text-gray-400">{r.line_items}</td>
            <td className="px-3 py-2 text-right tabular-nums">{fmtMoney(r.aws_total)}</td>
            <td className="px-3 py-2 text-right tabular-nums">{fmtMoney(r.gcp_od)}</td>
            <td className={`px-3 py-2 text-right tabular-nums ${r.diff >= 0 ? 'text-[#00C2BB]' : 'text-orange-400'}`}>
              {fmtMoney(r.diff)}
            </td>
            <td className={`px-3 py-2 text-right tabular-nums ${r.diff >= 0 ? 'text-[#00C2BB]' : 'text-orange-400'}`}>
              {fmtPct(r.diff_pct)}
            </td>
            {showShare && (
              <td className="px-3 py-2 text-right tabular-nums text-gray-400">
                {r.share_of_aws_spend != null ? fmtPct(r.share_of_aws_spend) : '—'}
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function SummaryBuilder({ jobs }: Readonly<{ jobs: Job[] }>) {
  const [query, setQuery] = useState('')
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [summary, setSummary] = useState<PortfolioSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const resultRef = useRef<HTMLDivElement | null>(null)

  const toggle = (id: string) => setChecked(prev => {
    const next = new Set(prev)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })

  const generate = async () => {
    setBusy(true); setError(null); setSummary(null)
    try {
      const s = await facetsGeneratePortfolioSummary(Array.from(checked))
      setSummary(s)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'summary generation failed')
    } finally {
      setBusy(false)
    }
  }

  // The job picklist above can run to dozens of rows, pushing the result well
  // below the fold — scroll it into view once it lands so "nothing happened"
  // isn't the first impression after a long wait.
  useEffect(() => {
    if (summary || error) resultRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [summary, error])

  const doneJobs = useMemo(() => jobs.filter(j => j.status === 'done'), [jobs])
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return doneJobs
    return doneJobs.filter(j => j.prospect.toLowerCase().includes(q) || j.owner.toLowerCase().includes(q))
  }, [doneJobs, query])

  return (
    <div className="space-y-6">
      {doneJobs.length === 0 ? (
        <p className="text-sm text-gray-500 py-8 text-center border border-white/10 rounded-xl">
          No completed reports yet — a job must finish before it can be included in a summary.
        </p>
      ) : (
        <>
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Filter by prospect or owner…"
            className="w-full sm:w-96 bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm
              placeholder:text-gray-500 transition-colors duration-150 focus:border-[#645DF6]"
          />
          {filtered.length === 0 ? (
            <p className="text-sm text-gray-500 py-6 text-center border border-white/10 rounded-xl">
              No reports match "{query}".
            </p>
          ) : (
            <div className="border border-white/10 rounded-xl overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-white/[0.03] text-xs uppercase tracking-wider text-gray-400">
                  <tr>
                    <th className="text-left px-3 py-2 w-10"></th>
                    <th className="text-left px-3 py-2">Prospect</th>
                    <th className="text-right px-3 py-2">AWS spend</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/5 max-h-96">
                  {filtered.map(j => (
                    <tr key={j.id} onClick={() => toggle(j.id)}
                        className="cursor-pointer hover:bg-white/[0.04] transition-colors duration-100">
                      <td className="px-3 py-2">
                        <input type="checkbox" readOnly checked={checked.has(j.id)} />
                      </td>
                      <td className="px-3 py-2 text-gray-200">{j.prospect}</td>
                      <td className="px-3 py-2 text-right tabular-nums text-gray-400">
                        {j.aws_spend != null ? fmtMoney(j.aws_spend) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <div className="flex items-center gap-3">
        <button
          onClick={generate}
          disabled={checked.size === 0 || busy}
          className="rounded-lg bg-[#645DF6] hover:bg-[#5750e0] disabled:opacity-40 disabled:cursor-not-allowed
            cursor-pointer disabled:cursor-not-allowed px-4 py-2 text-sm font-medium transition-colors duration-150"
        >
          {busy ? 'Generating…' : `Generate summary (${checked.size} selected)`}
        </button>
        {checked.size > 0 && (
          <a
            href={facetsPortfolioSummaryHTMLURL(Array.from(checked))}
            className="cursor-pointer rounded-lg border border-white/10 hover:border-[#645DF6]/50 hover:bg-white/[0.04]
              px-4 py-2 text-sm font-medium text-gray-300 hover:text-white transition-colors duration-150"
          >
            Download as HTML report ({checked.size})
          </a>
        )}
      </div>

      <div ref={resultRef} />
      {error && <p className="text-sm text-orange-400">{error}</p>}

      {summary && (
        <Reveal>
          <div className="space-y-8">
            <div>
              <h2 className="text-lg font-semibold mb-1">Consolidated AWS → GCP Cloud Cost Summary</h2>
              <p className="text-sm text-gray-500 mb-4">
                {summary.totals.customers} customers · {summary.totals.line_items} line items
              </p>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                <div className="bg-white/[0.03] border border-white/10 rounded-lg px-4 py-3">
                  <p className="text-xs text-gray-400 mb-1">AWS Total (Bill)</p>
                  <p className="text-xl font-semibold tabular-nums">{fmtMoney(summary.totals.aws_total)}</p>
                </div>
                <div className="bg-white/[0.03] border border-white/10 rounded-lg px-4 py-3">
                  <p className="text-xs text-gray-400 mb-1">AWS Mapped Cost</p>
                  <p className="text-xl font-semibold tabular-nums">{fmtMoney(summary.totals.aws_mapped_cost)}</p>
                </div>
                <div className="bg-white/[0.03] border border-white/10 rounded-lg px-4 py-3">
                  <p className="text-xs text-gray-400 mb-1">GCP On-Demand</p>
                  <p className="text-xl font-semibold tabular-nums text-[#00C2BB]">{fmtMoney(summary.totals.gcp_od)}</p>
                </div>
                <div className="bg-white/[0.03] border border-white/10 rounded-lg px-4 py-3">
                  <p className="text-xs text-gray-400 mb-1">Diff</p>
                  <p className="text-xl font-semibold tabular-nums text-[#00C2BB]">
                    {fmtMoney(summary.totals.diff)} ({fmtPct(summary.totals.diff_pct)})
                  </p>
                </div>
              </div>
            </div>

            <div>
              <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-2">Summary by Customer</h3>
              <div className="border border-white/10 rounded-xl overflow-hidden overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-white/[0.03] text-xs uppercase tracking-wider text-gray-400">
                    <tr>
                      <th className="text-left px-3 py-2">Customer</th>
                      <th className="text-right px-3 py-2">Items</th>
                      <th className="text-right px-3 py-2">AWS Total</th>
                      <th className="text-right px-3 py-2">Mapped</th>
                      <th className="text-right px-3 py-2">GCP OD</th>
                      <th className="text-right px-3 py-2">Diff</th>
                      <th className="text-right px-3 py-2">Diff %</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-white/5">
                    {summary.by_customer.map(c => (
                      <tr key={c.job_dir}>
                        <td className="px-3 py-2 text-gray-200">{c.customer}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-gray-400">{c.line_items}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{fmtMoney(c.aws_total)}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{fmtMoney(c.aws_mapped_cost)}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{fmtMoney(c.gcp_od)}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-[#00C2BB]">{fmtMoney(c.diff)}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-[#00C2BB]">{fmtPct(c.diff_pct)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            <div>
              <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-2">
                Summary by Category — all customers combined
              </h3>
              <div className="border border-white/10 rounded-xl overflow-hidden overflow-x-auto">
                <CategoryTable rows={summary.by_category} showShare />
              </div>
            </div>
          </div>
        </Reveal>
      )}
    </div>
  )
}

export function Facets({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Facets internal')
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [tab, setTab] = useState<'summary' | 'logs'>('summary')

  useEffect(() => { facetsListJobs().then(setJobs) }, [])

  return (
    <div className="min-h-screen text-white">
      <Nav user={user} />
      <div className="max-w-6xl mx-auto px-6 py-10">
        <div className="mb-8 anim-fade-in-up">
          <h1 className="text-2xl font-semibold">Facets internal tools</h1>
          <p className="text-sm text-gray-500 mt-1">
            Not linked anywhere else — visible only to @facets.cloud sessions.
          </p>
        </div>

        <div className="flex gap-2 mb-6 border-b border-white/10">
          {(['summary', 'logs'] as const).map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors duration-150 ${
                tab === t ? 'border-[#645DF6] text-white' : 'border-transparent text-gray-500 hover:text-gray-300'
              }`}
            >
              {t === 'summary' ? 'Portfolio Summary' : 'Job Logs'}
            </button>
          ))}
        </div>

        {jobs === null && <p className="text-sm text-gray-500 py-12 text-center">Loading…</p>}
        {jobs !== null && tab === 'summary' && <SummaryBuilder jobs={jobs} />}
        {jobs !== null && tab === 'logs' && <LogsPanel jobs={jobs} />}
      </div>
    </div>
  )
}
