import { useEffect, useState, useRef, useCallback } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { Layout } from '../components/Layout'
import { JobList } from '../components/JobList'
import { RunHistory } from '../components/RunHistory'
import { Summary } from '../components/Summary'
import { ContactCard } from '../components/ContactCard'
import { Reveal } from '../components/Reveal'
import { getJob, getProgress, listJobs, downloadURL, refineJob, retryJob, cancelJob, deleteJob, getRuns, getSummary, regenerateReport, recalculateJob, compareRuns } from '../api/jobs'
import { shareJob } from '../api/admin'
import type { Job, Progress, RunResult } from '../api/jobs'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'
import { TOTAL_PHASES } from '../lib/phases'
import { toast } from 'sonner'
import { JobHealth } from '../components/JobHealth'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { formatMoney } from '../lib/preferencesContext'
import { JobLogsViewer } from '../components/JobLogsViewer'
import { BackButton } from '../components/BackButton'

function pctVsAws(gcp: number, aws: number): { label: string; positive: boolean } | null {
  if (!aws || aws <= 0) return null
  const diff = (gcp - aws) / aws
  const sign = diff >= 0 ? '+' : ''
  return { label: `${sign}${Math.round(diff * 100)}%`, positive: diff < 0 }
}

function TotalsCard({ run, fallback, currency }: Readonly<{ run: RunResult | null; fallback: number | null; currency: { symbol: string; aws_fx_rate: number; fx_rate: number } }>) {
  const sym = currency.symbol || '$'
  const awsFx = currency.aws_fx_rate || 1.0
  const gcpFx = currency.fx_rate || 1.0
  if (!run) {
    return (
      <div className="bg-white/[0.02] border border-white/10 rounded-lg p-5 anim-fade-in-up">
        <h3 className="text-sm font-semibold text-[#00C2BB] uppercase tracking-wider mb-4">
          Cost projection
        </h3>
        <div className="flex justify-between items-center">
          <span className="text-gray-400">AWS Monthly Spend (pre-tax)</span>
          <span className="text-white font-semibold text-lg">
            {fallback != null ? formatMoney(fallback * awsFx, sym) : '—'}
          </span>
        </div>
      </div>
    )
  }
  const rows: Array<{ label: string; value: number; compare: boolean }> = [
    { label: 'AWS Mapped Cost',            value: run.aws_total,   compare: false },
    { label: 'GCP On-Demand',              value: run.gcp_od,      compare: true },
    { label: 'GCP 1-Year CUD',             value: run.gcp_1yr_cud, compare: true },
    { label: 'GCP 3-Year CUD',             value: run.gcp_3yr_cud, compare: true },
  ]
  return (
    <div className="bg-white/[0.02] border border-white/10 rounded-lg p-5 anim-fade-in-up card-lift">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-[#00C2BB] uppercase tracking-wider">
          Cost projection
        </h3>
        <span className="text-xs text-gray-400">Section A workload baseline</span>
      </div>
      <div className="divide-y divide-white/5">
        {rows.map((r, i) => {
          const pct = r.compare ? pctVsAws(r.value, run.aws_total) : null
          const isAws = r.label === 'AWS Mapped Cost'
          const fx = isAws ? awsFx : gcpFx
          return (
            <div key={r.label}
              className="flex flex-col sm:flex-row sm:items-center sm:justify-between py-2.5 gap-1 anim-fade-in-up"
              style={{ animationDelay: `${i * 70}ms` }}>
              <span className="text-gray-400 text-sm">{r.label}</span>
              <div className="flex items-baseline gap-3">
                <span className="font-semibold text-white tabular-nums">
                  {formatMoney(r.value * fx, sym)}
                </span>
                {pct && (
                  <span className="text-xs font-medium px-1.5 py-0.5 rounded-full bg-white/5 text-gray-400">
                    {pct.label}
                  </span>
                )}
              </div>
            </div>
          )
        })}
      </div>
      <p className="mt-3 text-xs text-gray-500 leading-relaxed border-t border-white/5 pt-2">
        * Priced workload baseline matches Section A headline cards. Non-workload &amp; unpriced items carry 1:1 passthroughs in report Section B.
      </p>
    </div>
  )
}

const DECODE_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'

type CharState = { ch: string; cycling: boolean }

function GlitchText({ text }: { text: string }) {
  const [display, setDisplay] = useState<CharState[]>(() =>
    text.split('').map(ch => ({ ch, cycling: false }))
  )
  const rafRef = useRef<number | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const decode = useCallback((target: string, onDone: () => void) => {
    const src = target.split('')
    const start = performance.now()
    // Each position resolves in a staggered left-to-right wave with slight jitter
    const resolveTimes = src.map((ch, i) =>
      ch === ' ' ? 0 : 100 + i * 28 + Math.random() * 55
    )

    const tick = (now: number) => {
      const elapsed = now - start
      let allDone = true
      const next = src.map((ch, i) => {
        if (ch === ' ') return { ch, cycling: false }
        if (elapsed >= resolveTimes[i]) return { ch, cycling: false }
        allDone = false
        return {
          ch: DECODE_CHARS[Math.floor(Math.random() * DECODE_CHARS.length)],
          cycling: true,
        }
      })
      setDisplay(next)
      if (allDone) {
        onDone()
      } else {
        rafRef.current = requestAnimationFrame(tick)
      }
    }

    rafRef.current = requestAnimationFrame(tick)
  }, [])

  useEffect(() => {
    setDisplay(text.split('').map(ch => ({ ch, cycling: false })))
    if (rafRef.current) cancelAnimationFrame(rafRef.current)
    if (timerRef.current) clearTimeout(timerRef.current)

    const schedule = () => {
      timerRef.current = setTimeout(() => decode(text, () => {
        timerRef.current = setTimeout(schedule, 5000)
      }), 5000)
    }

    timerRef.current = setTimeout(() => decode(text, () => {
      timerRef.current = setTimeout(schedule, 5000)
    }), 1200)

    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [text, decode])

  return (
    <span>
      {display.map((c, i) => (
        <span
          key={i}
          style={c.cycling ? { opacity: 0.4, color: '#00C2BB' } : undefined}
        >{c.ch}</span>
      ))}
    </span>
  )
}

type WrongCloudInfo = {
  cloud: string
  icon: string
  color: string
  accent: string
}

function detectWrongCloud(err: string): WrongCloudInfo | null {
  const e = err.toLowerCase()
  if (e.includes('azure billing export')) {
    return { cloud: 'Azure', icon: '☁', color: 'bg-blue-500/10 border-blue-500/30', accent: 'text-blue-400' }
  }
  if (e.includes('gcp billing export')) {
    return { cloud: 'Google Cloud', icon: '🔵', color: 'bg-red-400/10 border-red-400/30', accent: 'text-red-400' }
  }
  if (e.includes('vmware') || e.includes('vcenter') || e.includes('rvtools')) {
    return { cloud: 'VMware / vCenter', icon: '🖥', color: 'bg-gray-400/10 border-gray-400/30', accent: 'text-gray-300' }
  }
  if (e.includes('oracle cloud') || e.includes('oci')) {
    return { cloud: 'Oracle Cloud (OCI)', icon: '🔴', color: 'bg-red-500/10 border-red-500/30', accent: 'text-red-400' }
  }
  return null
}

function WrongCloudCard({ info }: Readonly<{ info: WrongCloudInfo }>) {
  return (
    <div className={`${info.color} border rounded-xl p-5 text-sm space-y-4 anim-scale-in`}>
      <div className="flex items-start gap-3">
        <span className="text-2xl leading-none mt-0.5 select-none" aria-hidden="true">{info.icon}</span>
        <div>
          <p className={`font-semibold text-base ${info.accent}`}>
            {info.cloud} bill detected
          </p>
          <p className="text-gray-400 mt-1 leading-relaxed">
            This tool maps <span className="text-white font-medium">AWS</span> costs to their equivalent{' '}
            <span className="text-white font-medium">Google Cloud</span> services — it cannot process {info.cloud} exports.
          </p>
        </div>
      </div>

      <div className="bg-white/[0.04] rounded-lg p-4 space-y-3">
        <p className="text-xs uppercase tracking-wider text-gray-500 font-medium">What to upload instead</p>
        <ul className="space-y-2 text-gray-300 text-xs leading-relaxed">
          <li className="flex items-start gap-2">
            <span className="text-[#00C2BB] mt-0.5 shrink-0">✓</span>
            <span><span className="text-white font-medium">AWS Cost & Usage Report (CUR)</span> — the standard export from AWS Billing → Cost & Usage Reports</span>
          </li>
          <li className="flex items-start gap-2">
            <span className="text-[#00C2BB] mt-0.5 shrink-0">✓</span>
            <span><span className="text-white font-medium">AWS Cost Explorer CSV</span> — exported from the Cost Explorer console</span>
          </li>
          <li className="flex items-start gap-2">
            <span className="text-[#00C2BB] mt-0.5 shrink-0">✓</span>
            <span><span className="text-white font-medium">AWS estimated-bill PDF</span> — the monthly bill PDF from AWS Billing</span>
          </li>
        </ul>
      </div>

      <Link
        to="/"
        className="btn-shimmer w-full py-2.5 rounded-lg font-medium text-white text-center block text-sm">
        ← Upload an AWS Bill
      </Link>
    </div>
  )
}

const PHASE_LABELS: Record<number, string> = {
  1: 'Loading and Classifying Your AWS Bill',
  2: 'Mapping AWS Line Items to GCP Services',
  3: 'Reviewing Mappings for Sanity',
  4: 'Applying GCP Pricing (On-Demand + CUDs)',
  5: 'Investigating Anomalies',
  6: 'Generating the HTML Report',
}

function PhaseProgress({ phase }: Readonly<{ phase: number }>) {
  const label = PHASE_LABELS[phase] ?? 'Initializing…'
  return (
    <div className="mt-3 pt-3 border-t border-[#645DF6]/20">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs uppercase tracking-wider text-gray-400">Currently</span>
        <span className="text-xs text-gray-500 tabular-nums">Step {phase} of {TOTAL_PHASES}</span>
      </div>
      <p key={label} className="phase-label-enter text-sm text-gray-200"><GlitchText text={label} /></p>
      <div className="mt-3 flex gap-1">
        {[1, 2, 3, 4, 5, 6].map(n => (
          <div key={n}
            className={`h-1.5 flex-1 rounded-full overflow-hidden ${n > phase ? 'bg-white/10' : ''}`}>
            {n <= phase && (
              <div className={`h-full w-full rounded-full bg-[#00C2BB] ${
                n === phase ? 'animate-pulse' : 'phase-bar-done'
              }`}
                style={n < phase ? { animationDelay: `${(n - 1) * 80}ms` } : undefined} />
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

function JobActivityCard({ job, progress }: Readonly<{ job: Job; progress: Progress | null }>) {
  const durations = progress?.phase_durations
  return (
    <div className="bg-white/[0.02] border border-white/10 rounded-xl p-4 text-xs space-y-3 anim-fade-in">
      <h3 className="font-semibold text-gray-300 uppercase tracking-wider text-[11px] flex items-center justify-between">
        <span>Job Activity &amp; Provenance</span>
        <span className="text-gray-500 font-mono text-[10px]">{job.id.slice(0, 8)}</span>
      </h3>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-gray-400">
        <div>
          <span className="text-gray-500 block text-[10px] uppercase">Creator</span>
          <span className="text-gray-200 truncate block font-medium" title={job.owner}>{job.owner}</span>
        </div>
        <div>
          <span className="text-gray-500 block text-[10px] uppercase">Created</span>
          <span className="text-gray-200 block font-mono">{new Date(job.created_at).toLocaleString()}</span>
        </div>
        <div>
          <span className="text-gray-500 block text-[10px] uppercase">Engine</span>
          <span className="text-[#00C2BB] block font-mono font-medium">{job.engine || 'gemini-3.8-flash'}{job.engine_version ? ` (${job.engine_version})` : ''}</span>
        </div>
        <div>
          <span className="text-gray-500 block text-[10px] uppercase">Assumptions</span>
          <span className="text-gray-200 block font-mono">{job.assumptions_version || 'v1-default'}</span>
        </div>
      </div>
      {durations && Object.keys(durations).length > 0 && (
        <div className="pt-2 border-t border-white/5 space-y-1.5">
          <span className="text-gray-500 block text-[10px] uppercase font-semibold">Phase Durations</span>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
            {Object.entries(durations)
              .sort(([a], [b]) => Number(a) - Number(b))
              .map(([phaseNum, info]) => {
                const p = info as { name?: string; duration_seconds?: number }
                return (
                  <div key={phaseNum} className="bg-white/[0.02] border border-white/5 rounded px-2 py-1.5 flex items-center justify-between">
                    <span className="text-gray-400 truncate pr-1">P{phaseNum}: {p.name || PHASE_LABELS[Number(phaseNum)] || 'Phase'}</span>
                    <span className="text-gray-200 font-mono text-[10px] shrink-0">
                      {p.duration_seconds !== undefined ? `${p.duration_seconds}s` : '✓'}
                    </span>
                  </div>
                )
              })}
          </div>
        </div>
      )}
    </div>
  )
}

export function JobStatus({ user }: Readonly<{ user: UserInfo }>) {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [job, setJob] = useState<Job | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [progress, setProgress] = useState<Progress | null>(null)
  const [runs, setRuns] = useState<RunResult[]>([])
  const [summary, setSummary] = useState<string | null>(null)
  const [currency, setCurrency] = useState<{ symbol: string; aws_fx_rate: number; fx_rate: number }>({ symbol: '$', aws_fx_rate: 1.0, fx_rate: 1.0 })
  const [refineOpen, setRefineOpen] = useState(false)
  const [instruction, setInstruction] = useState('')
  const [refining, setRefining] = useState(false)
  const [refineError, setRefineError] = useState<string | null>(null)
  const [retrying, setRetrying] = useState(false)
  const [retryError, setRetryError] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)

  const [cancelling, setCancelling] = useState(false)
  const [cancelError, setCancelError] = useState<string | null>(null)
  const [regenerating, setRegenerating] = useState(false)
  const [regenerateError, setRegenerateError] = useState<string | null>(null)
  const [recalculating, setRecalculating] = useState(false)
  const [recalculateError, setRecalculateError] = useState<string | null>(null)
  const [comparing, setComparing] = useState(false)
  const [compareError, setCompareError] = useState<string | null>(null)
  const [comparison, setComparison] = useState<any>(null)
  const [sharing, setSharing] = useState(false)
  const [shareUrl, setShareUrl] = useState<string | null>(null)

  const submitShare = async () => {
    if (!id) return
    setSharing(true)
    try {
      const res = await shareJob(id)
      setShareUrl(res.url)
      const fullUrl = `${window.location.origin}${res.url}`
      await navigator.clipboard.writeText(fullUrl)
      toast.success('Share link copied to clipboard (valid 90 days)')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Share failed')
    } finally {
      setSharing(false)
    }
  }

  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false)
  const [cancelConfirmOpen, setCancelConfirmOpen] = useState(false)

  const submitDelete = async () => {
    if (!id || !job) return
    setDeleting(true)
    try {
      await deleteJob(id)
      toast.success(`Job run for "${job.prospect}" deleted`)
      setDeleteConfirmOpen(false)
      navigate('/', { replace: true })
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Delete failed')
      setDeleting(false)
    }
  }

  const submitCancel = async () => {
    if (!id) return
    setCancelling(true)
    setCancelError(null)
    try {
      await cancelJob(id)
      setJob(prev => prev ? { ...prev, status: 'cancelled' } : prev)
      setJobs(prev => prev.map(old => old.id === id ? { ...old, status: 'cancelled' } : old))
      setCancelConfirmOpen(false)
    } catch (e: unknown) {
      setCancelError(e instanceof Error ? e.message : String(e))
    } finally {
      setCancelling(false)
    }
  }

  const submitRegenerate = async () => {
    if (!id) return
    setRegenerating(true)
    setRegenerateError(null)
    try {
      const result = await regenerateReport(id)
      if (result) {
        toast.success('Report regenerated')
        setRuns(prev => [...prev, { ...result, run_id: result.run_id } as any])
      } else {
        setRegenerateError('Regenerate failed')
      }
    } catch (e: unknown) {
      setRegenerateError(e instanceof Error ? e.message : String(e))
    } finally {
      setRegenerating(false)
    }
  }

  const submitRecalculate = async () => {
    if (!id) return
    setRecalculating(true)
    setRecalculateError(null)
    try {
      const result = await recalculateJob(id)
      if (result) {
        toast.success('Job recalculated and report regenerated')
        setRuns(prev => [...prev, { ...result, run_id: result.run_id } as any])
      } else {
        setRecalculateError('Recalculate failed')
      }
    } catch (e: unknown) {
      setRecalculateError(e instanceof Error ? e.message : String(e))
    } finally {
      setRecalculating(false)
    }
  }

  const submitCompare = async () => {
    if (!id || runs.length < 2) return
    setComparing(true)
    setCompareError(null)
    setComparison(null)
    try {
      const runIds = runs.map(r => r.run_id)
      const from = runIds[runIds.length - 2]
      const to = runIds[runIds.length - 1]
      const result = await compareRuns(id, from, to)
      if (result) {
        setComparison(result)
      } else {
        setCompareError('Comparison failed')
      }
    } catch (e: unknown) {
      setCompareError(e instanceof Error ? e.message : String(e))
    } finally {
      setComparing(false)
    }
  }

  const submitRetry = async () => {
    if (!id) return
    setRetrying(true)
    setRetryError(null)
    try {
      await retryJob(id)
      setJob(prev => prev ? { ...prev, status: 'running', error: '', aws_spend: null } : prev)
      setProgress(null)
      setRuns([])
      setSummary(null)
    } catch (e: unknown) {
      setRetryError(e instanceof Error ? e.message : String(e))
    } finally {
      setRetrying(false)
    }
  }

  const submitRefine = async () => {
    if (!id || instruction.trim().length < 3) return
    setRefining(true)
    setRefineError(null)
    try {
      await refineJob(id, instruction.trim())
      // Optimistic flip — the backend has already set status=running before
      // returning 202, but updating local state in the same batch avoids the
      // flicker of the done card briefly reappearing.
      setJob(prev => prev ? { ...prev, status: 'running', aws_spend: null, error: '' } : prev)
      setProgress(null)
      setRuns([])
      setSummary(null)
      setRefineOpen(false)
      setInstruction('')
    } catch (e: unknown) {
      setRefineError(e instanceof Error ? e.message : String(e))
    } finally {
      setRefining(false)
    }
  }

  useTitle(job ? `${job.prospect} · ${job.status.charAt(0).toUpperCase() + job.status.slice(1)}` : 'Loading')

  useEffect(() => { listJobs().then(setJobs) }, [])

  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    let cancelled = false
    let prevStatus: string | null = null
    const poll = async () => {
      if (cancelled) return
      try {
        const j = await getJob(id)
        if (cancelled) return
        setJob(j)
        setLoadError(null)
        setJobs(prev => prev.map(old => old.id === j.id ? { ...old, status: j.status } : old))
        if (j.status === 'running' || j.status === 'pending') {
          getProgress(id).then(p => { if (!cancelled && p) setProgress(p) })
          setTimeout(poll, 5000)
        } else if (j.status === 'done' && prevStatus !== 'done') {
          // First time we see done — fetch runs + summary.
          getRuns(id).then(r => { if (!cancelled) { setRuns(r.runs); setCurrency({ symbol: r.currency.symbol, aws_fx_rate: r.currency.aws_fx_rate, fx_rate: r.currency.fx_rate }) } })
          getSummary(id).then(s => { if (!cancelled) setSummary(s) })
        }
        prevStatus = j.status
      } catch (err: unknown) {
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : 'Job not found')
        }
      }
    }
    poll()
    return () => { cancelled = true }
  }, [id, refining, retrying])

  if (!job) {
    return (
      <Layout user={user}>
        <div className="max-w-xl mx-auto px-6 py-20 text-center">
          {loadError ? (
            <div className="p-6 bg-red-500/10 border border-red-500/30 rounded-xl space-y-4 anim-scale-in">
              <p className="text-red-400 font-medium">Failed to load report</p>
              <p className="text-xs text-gray-400">{loadError}</p>
              <Link to="/" className="inline-block px-4 py-2 bg-white/10 hover:bg-white/20 rounded-lg text-xs font-semibold transition-colors">
                ← Back to Upload
              </Link>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-3">
              <span className="relative flex w-3 h-3">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#645DF6] opacity-75" />
                <span className="relative inline-flex rounded-full w-3 h-3 bg-[#645DF6]" />
              </span>
              <p className="text-sm text-gray-400">Loading estimation report…</p>
            </div>
          )}
        </div>
      </Layout>
    )
  }

  return (
    <Layout user={user}>
      <div className="max-w-5xl mx-auto px-6 py-10 grid gap-8 lg:grid-cols-[1fr_320px]">
        <main className="flex flex-col gap-6">
          <BackButton fallback="/" />
          <div className="flex flex-wrap items-center justify-between gap-3 anim-fade-in-up">
            <h1 className="text-2xl font-semibold">
              Report for <span className="text-[#645DF6]">{job.prospect}</span>
            </h1>
            {job.engine && (
              <span className="text-xs px-2.5 py-1 rounded-md bg-white/[0.04] border border-white/10 text-gray-300 font-mono flex items-center gap-1.5" title={`Engine: ${job.engine}${job.engine_version ? ` (${job.engine_version})` : ''}`}>
                <span className="w-1.5 h-1.5 rounded-full bg-[#00C2BB]"></span>
                <span>{job.engine}</span>
                {job.engine_version && <span className="text-gray-500 text-[10px]">v{job.engine_version}</span>}
              </span>
            )}
          </div>

          <JobActivityCard job={job} progress={progress} />

          {(job.status === 'pending' || job.status === 'running') && (
            <div className="bg-[#645DF6]/10 border border-[#645DF6]/30 rounded-lg p-4 text-sm anim-scale-in">
              <div className="flex items-center gap-2 mb-2">
                <span className="relative flex w-2.5 h-2.5">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#00C2BB] opacity-60" />
                  <span className="relative inline-flex rounded-full w-2.5 h-2.5 bg-[#00C2BB]" />
                </span>
                <strong className="text-[#00C2BB]">AI Agent Running</strong>
              </div>
              <p className="text-gray-300">An AI agent is analyzing your bill and mapping each AWS line item to its GCP equivalent. This typically takes 10–30 minutes.</p>
              <p className="text-gray-400 text-xs mt-1">We'll email you when it's ready — you can close this tab.</p>
              <PhaseProgress phase={progress?.phase_number || 1} />
              <div className="mt-4 pt-3 border-t border-[#645DF6]/20 flex items-center justify-between">
                {cancelError && <span className="text-xs text-orange-400">{cancelError}</span>}
                <button
                  onClick={() => setCancelConfirmOpen(true)}
                  disabled={cancelling}
                  className="px-3 py-1.5 rounded-lg border border-orange-500/30 text-xs font-medium text-orange-400 hover:bg-orange-500/10 disabled:opacity-40 transition-colors ml-auto cursor-pointer"
                >
                  {cancelling ? 'Stopping…' : 'Cancel / Stop'}
                </button>
              </div>
            </div>
          )}

          {job.status === 'done' && (
            <>
              <TotalsCard run={runs[0] ?? null} fallback={job.aws_spend} currency={currency} />

              <p className="text-xs text-gray-500 leading-relaxed -mt-2 anim-fade-in">
                AI-generated estimate — verify before sharing. If a mapping looks off,
                hit "Refine this report" below and tell the agent what to change.
              </p>

              {summary && (
                <Reveal>
                  <Summary markdown={summary} />
                </Reveal>
              )}

              <div className="flex flex-col gap-2 anim-fade-in-up delay-175">
                <p className="text-xs uppercase tracking-wider text-gray-500">Download report &amp; Bill</p>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                  <a href={downloadURL(job.id)}
                    className="btn-shimmer py-2.5 rounded-lg font-medium text-white text-center text-sm block">
                    ↓ HTML Report
                  </a>
                  <a href={downloadURL(job.id, undefined, 'xlsx')}
                    className="rounded-lg border border-white/10 hover:border-[#645DF6]/50 hover:bg-white/[0.04]
                      py-2.5 font-medium text-gray-300 hover:text-white text-center text-sm block transition-colors duration-150">
                    ↓ Excel (.xlsx)
                  </a>
                  <a href={downloadURL(job.id, undefined, 'csv')}
                    className="rounded-lg border border-white/10 hover:border-[#645DF6]/50 hover:bg-white/[0.04]
                      py-2.5 font-medium text-gray-300 hover:text-white text-center text-sm block transition-colors duration-150">
                    ↓ CSV (Summary)
                  </a>
                  <a href={downloadURL(job.id, undefined, 'csv', 'line-item')}
                    className="rounded-lg border border-white/10 hover:border-[#645DF6]/50 hover:bg-white/[0.04]
                      py-2.5 font-medium text-gray-300 hover:text-white text-center text-sm block transition-colors duration-150"
                    title="Detailed per-line-item CSV mapping export">
                    ↓ CSV (Line Items)
                  </a>
                  <a href={downloadURL(job.id, undefined, 'json')}
                    className="rounded-lg border border-white/10 hover:border-[#645DF6]/50 hover:bg-white/[0.04]
                      py-2.5 font-medium text-gray-300 hover:text-white text-center text-sm block transition-colors duration-150">
                    ↓ JSON
                  </a>
                  <a href={downloadURL(job.id, undefined, 'bill')}
                    className="rounded-lg border border-[#00C2BB]/30 text-[#00C2BB] hover:bg-[#00C2BB]/10
                      py-2.5 font-medium text-center text-sm block transition-colors duration-150"
                    title="Download original uploaded AWS Bill file">
                    ↓ Original Bill
                  </a>
                </div>
                <div className="mt-2 flex items-center justify-between gap-3">
                  <button
                    onClick={submitShare}
                    disabled={sharing}
                    className="text-xs px-3 py-1.5 rounded-lg border border-white/10 hover:bg-white/5 text-gray-300 hover:text-white transition-colors cursor-pointer flex items-center gap-1.5"
                  >
                    <span>🔗</span>
                    <span>{sharing ? 'Generating link…' : 'Share Report Link (90-day)'}</span>
                  </button>
                  {shareUrl && (
                    <span className="text-xs text-[#00C2BB] truncate">
                      Link copied to clipboard! ({window.location.origin}{shareUrl})
                    </span>
                  )}
                </div>
              </div>

              {user.is_admin && (
                <>
                  <div className="flex flex-col gap-2 anim-fade-in-up delay-175">
                    <p className="text-xs uppercase tracking-wider text-gray-500">Admin actions</p>
                    <button
                      onClick={submitRegenerate}
                      disabled={regenerating}
                      className="w-full py-2.5 rounded-lg border border-[#645DF6]/30 text-[#645DF6] text-xs font-medium hover:bg-[#645DF6]/10 disabled:opacity-40 transition-colors"
                    >
                      {regenerating ? 'Regenerating…' : 'Regenerate Report'}
                    </button>
                    {regenerateError && <p className="text-xs text-orange-400">{regenerateError}</p>}
                    <button
                      onClick={submitRecalculate}
                      disabled={recalculating}
                      className="w-full py-2.5 rounded-lg border border-[#00C2BB]/30 text-[#00C2BB] text-xs font-medium hover:bg-[#00C2BB]/10 disabled:opacity-40 transition-colors"
                      title="Re-run deterministic mapping and pricing pipeline with latest assumptions and rules without re-running ingest or LLM"
                    >
                      {recalculating ? 'Recalculating…' : 'Recalculate (Deterministic Chain)'}
                    </button>
                    {recalculateError && <p className="text-xs text-orange-400">{recalculateError}</p>}
                    <button
                      onClick={submitCompare}
                      disabled={comparing || runs.length < 2}
                      className="w-full py-2.5 rounded-lg border border-[#00C2BB]/30 text-[#00C2BB] text-xs font-medium hover:bg-[#00C2BB]/10 disabled:opacity-40 transition-colors"
                    >
                      {comparing ? 'Comparing…' : 'Compare Runs'}
                    </button>
                    {compareError && <p className="text-xs text-orange-400">{compareError}</p>}
                    {comparison && (
                      <div className="mt-3 bg-white/[0.02] border border-white/10 rounded-lg p-4 text-sm">
                        <h4 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">
                          Run Comparison
                        </h4>
                        <table className="w-full text-left text-xs">
                          <thead>
                            <tr className="border-b border-white/10">
                              <th className="pb-2 text-gray-400">Metric</th>
                              <th className="pb-2 text-right text-gray-400">
                                Run A ({comparison.run_a.run_id.slice(-8)})
                              </th>
                              <th className="pb-2 text-right text-gray-400">
                                Run B ({comparison.run_b.run_id.slice(-8)})
                              </th>
                              <th className="pb-2 text-right text-gray-400">Delta</th>
                            </tr>
                          </thead>
                          <tbody>
                            <tr className="border-b border-white/5">
                              <td className="py-2 text-gray-400">AWS Cost</td>
                              <td className="py-2 text-right font-mono">
                                {comparison.run_a.aws_total != null ? formatMoney(comparison.run_a.aws_total * currency.aws_fx_rate, currency.symbol) : '—'}
                              </td>
                              <td className="py-2 text-right font-mono">
                                {comparison.run_b.aws_total != null ? formatMoney(comparison.run_b.aws_total * currency.aws_fx_rate, currency.symbol) : '—'}
                              </td>
                              <td className="py-2 text-right font-mono">
                                {comparison.aws_delta >= 0 ? '+' : '-'}{formatMoney(Math.abs(comparison.aws_delta) * currency.aws_fx_rate, currency.symbol)}
                              </td>
                            </tr>
                            <tr className="border-b border-white/5">
                              <td className="py-2 text-gray-400">GCP On-Demand</td>
                              <td className="py-2 text-right font-mono">
                                {comparison.run_a.gcp_od != null ? formatMoney(comparison.run_a.gcp_od * currency.fx_rate, currency.symbol) : '—'}
                              </td>
                              <td className="py-2 text-right font-mono">
                                {comparison.run_b.gcp_od != null ? formatMoney(comparison.run_b.gcp_od * currency.fx_rate, currency.symbol) : '—'}
                              </td>
                              <td className="py-2 text-right font-mono">
                                {comparison.gcp_od_delta >= 0 ? '+' : '-'}{formatMoney(Math.abs(comparison.gcp_od_delta) * currency.fx_rate, currency.symbol)}
                              </td>
                            </tr>
                            <tr className="border-b border-white/5">
                              <td className="py-2 text-gray-400">GCP 1-Yr CUD</td>
                              <td className="py-2 text-right font-mono">
                                {comparison.run_a.gcp_1yr_cud != null ? formatMoney(comparison.run_a.gcp_1yr_cud * currency.fx_rate, currency.symbol) : '—'}
                              </td>
                              <td className="py-2 text-right font-mono">
                                {comparison.run_b.gcp_1yr_cud != null ? formatMoney(comparison.run_b.gcp_1yr_cud * currency.fx_rate, currency.symbol) : '—'}
                              </td>
                              <td className="py-2 text-right font-mono">
                                {comparison.gcp_1yr_cud_delta >= 0 ? '+' : '-'}{formatMoney(Math.abs(comparison.gcp_1yr_cud_delta) * currency.fx_rate, currency.symbol)}
                              </td>
                            </tr>
                            <tr>
                              <td className="py-2 text-gray-400">GCP 3-Yr CUD</td>
                              <td className="py-2 text-right font-mono">
                                {comparison.run_a.gcp_3yr_cud != null ? formatMoney(comparison.run_a.gcp_3yr_cud * currency.fx_rate, currency.symbol) : '—'}
                              </td>
                              <td className="py-2 text-right font-mono">
                                {comparison.run_b.gcp_3yr_cud != null ? formatMoney(comparison.run_b.gcp_3yr_cud * currency.fx_rate, currency.symbol) : '—'}
                              </td>
                              <td className="py-2 text-right font-mono">
                                {comparison.gcp_3yr_cud_delta >= 0 ? '+' : '-'}{formatMoney(Math.abs(comparison.gcp_3yr_cud_delta) * currency.fx_rate, currency.symbol)}
                              </td>
                            </tr>
                          </tbody>
                        </table>
                        {comparison.assumptions_version_a && (
                          <p className="text-xs text-gray-400 mt-3">
                            Assumptions version:{' '}
                            {comparison.assumptions_version_a}{' → '}{comparison.assumptions_version_b}
                          </p>
                        )}
                        {comparison.catalog_vintage_a && (
                          <p className="text-xs text-gray-400">
                            Catalog:{' '}
                            {comparison.catalog_vintage_a}{' → '}{comparison.catalog_vintage_b}
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                  <JobHealth jobId={job.id} />
                </>
              )}

              <Reveal delay={60}>
                <div>
                  <p className="text-xs uppercase tracking-wider text-gray-500 mb-2">Export history</p>
                  <RunHistory jobId={job.id} runs={runs} />
                </div>
              </Reveal>

              {!refineOpen ? (
                <Reveal delay={120}>
                <button
                  onClick={() => setRefineOpen(true)}
                  className="nav-link text-sm text-[#645DF6] hover:text-[#7d77f8] self-start transition-colors duration-150">
                  Refine This Report →
                </button>
                </Reveal>
              ) : (
                <div className="bg-white/[0.02] border border-white/10 rounded-lg p-5 space-y-3 anim-scale-in">
                  <div>
                    <h3 className="text-sm font-semibold text-[#00C2BB] uppercase tracking-wider mb-1">
                      Refine the projection
                    </h3>
                    <p className="text-xs text-gray-500">
                      Tell the AI what to change. It will resume the same session, update mappings, recompute,
                      and rewrite the report. Original is preserved if refinement fails.
                    </p>
                  </div>
                  <textarea
                    value={instruction}
                    onChange={e => setInstruction(e.target.value)}
                    placeholder="e.g. Map gp3 EBS volumes to pd-standard instead of pd-ssd."
                    rows={3}
                    className="w-full bg-white/5 border border-white/20 rounded-lg px-3 py-2 text-sm outline-none
                      focus:border-[#645DF6] transition-colors duration-150 resize-y"
                  />
                  {refineError && <p className="text-xs text-orange-400">{refineError}</p>}
                  <div className="flex gap-2 justify-end">
                    <button
                      onClick={() => { setRefineOpen(false); setInstruction(''); setRefineError(null) }}
                      className="px-4 py-2 text-sm text-gray-400 hover:text-gray-200 transition-colors duration-150">
                      Cancel
                    </button>
                    <button
                      onClick={submitRefine}
                      disabled={refining || instruction.trim().length < 3}
                      className="btn-shimmer px-4 py-2 rounded-lg text-sm font-medium text-white
                        disabled:opacity-40 disabled:cursor-not-allowed">
                      {refining ? 'Submitting…' : 'Submit refinement'}
                    </button>
                  </div>
                </div>
              )}
            </>
          )}

          {job.status === 'failed' && (() => {
            const err = job.error ?? ''
            const wrongCloud = detectWrongCloud(err)
            if (wrongCloud) {
              return <WrongCloudCard info={wrongCloud} />
            }
            return (
              <div className="bg-orange-400/10 border border-orange-400/30 rounded-lg p-4 text-sm space-y-3 anim-scale-in">
                <div>
                  <strong className="text-orange-400">Report generation failed</strong>
                  {job.error && (
                    <pre className="text-gray-400 text-xs mt-1 font-mono whitespace-pre-wrap break-words max-h-80 overflow-auto">{job.error}</pre>
                  )}
                </div>
                {retryError && <p className="text-xs text-orange-400">{retryError}</p>}
                <button
                  onClick={submitRetry}
                  disabled={retrying}
                  className="btn-shimmer px-4 py-2 rounded-lg text-sm font-medium text-white
                    disabled:opacity-40 disabled:cursor-not-allowed">
                  {retrying ? 'Retrying…' : '↻ Retry'}
                </button>
              </div>
            )
          })()}

          {job.status === 'cancelled' && (
            <div className="bg-white/[0.03] border border-white/10 rounded-lg p-5 text-sm space-y-3 anim-scale-in">
              <div className="flex items-center gap-2">
                <span className="text-gray-400 font-bold text-base">⊘</span>
                <strong className="text-gray-300">Estimation stopped</strong>
                <span className="text-xs px-2 py-0.5 rounded bg-white/10 text-gray-400 font-mono">cancelled</span>
              </div>
              <p className="text-gray-400 text-xs">
                This run was cancelled. No further phases will be executed.
              </p>
            </div>
          )}

          <Reveal delay={150}>
            <div>
              <p className="text-xs uppercase tracking-wider text-gray-500 mb-2">Execution Logs</p>
              <JobLogsViewer jobId={job.id} isRunning={job.status === 'running' || job.status === 'pending'} />
            </div>
          </Reveal>

          <div className="flex items-center justify-between pt-2 border-t border-white/5">
            <Link to="/" className="nav-link text-xs text-[#645DF6]">← New Estimation</Link>
            {(job.status === 'done' || job.status === 'failed' || job.status === 'cancelled') && (
              <button
                type="button"
                onClick={() => setDeleteConfirmOpen(true)}
                disabled={deleting}
                className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-red-400 transition-colors duration-150 disabled:opacity-40 cursor-pointer"
              >
                <svg className="w-3.5 h-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor">
                  <path d="M2.5 4h11m-9.5 0v9a1 1 0 001 1h6a1 1 0 001-1v-9M5.5 4V2.5a1 1 0 011-1h3a1 1 0 011 1V4" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
                <span>{deleting ? 'Deleting…' : 'Delete Job Run'}</span>
              </button>
            )}
          </div>
        </main>
        <aside className="lg:border-l lg:border-white/10 lg:pl-8 flex flex-col gap-6">
          <div className="anim-fade-in delay-175">
            <p className="text-xs uppercase tracking-wider text-gray-400 mb-3">Your Reports</p>
            <JobList jobs={jobs} />
          </div>
          <div className="anim-fade-in delay-250">
            <ContactCard />
          </div>
        </aside>
      </div>

      <ConfirmDialog
        open={deleteConfirmOpen}
        onOpenChange={setDeleteConfirmOpen}
        title="Delete job run"
        description={`Delete this job run for "${job.prospect}"? It will be removed from your active reports.`}
        confirmLabel="Delete run"
        destructive
        loading={deleting}
        onConfirm={submitDelete}
      />

      <ConfirmDialog
        open={cancelConfirmOpen}
        onOpenChange={setCancelConfirmOpen}
        title="Stop estimation run"
        description="Stop this estimation run? No further phases will be executed."
        confirmLabel="Stop run"
        destructive
        loading={cancelling}
        onConfirm={submitCancel}
      />
    </Layout>
  )
}
