import { useEffect, useState, useRef, useCallback } from 'react'
import { useParams, Link } from 'react-router-dom'
import { Nav } from '../components/Nav'
import { JobList } from '../components/JobList'
import { RunHistory } from '../components/RunHistory'
import { Summary } from '../components/Summary'
import { ContactCard } from '../components/ContactCard'
import { Reveal } from '../components/Reveal'
import { getJob, getProgress, listJobs, downloadURL, refineJob, retryJob, getRuns, getSummary } from '../api/jobs'
import type { Job, Progress, RunResult } from '../api/jobs'
import { shareJob } from '../api/admin'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'
import { TOTAL_PHASES } from '../lib/phases'

function formatDollars(n: number): string {
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function pctVsAws(gcp: number, aws: number): { label: string; positive: boolean } | null {
  if (!aws || aws <= 0) return null
  const diff = (gcp - aws) / aws
  const sign = diff >= 0 ? '+' : ''
  return { label: `${sign}${Math.round(diff * 100)}%`, positive: diff < 0 }
}

function TotalsCard({ run, fallback }: Readonly<{ run: RunResult | null; fallback: number | null }>) {
  if (!run) {
    return (
      <div className="bg-white/[0.02] border border-white/10 rounded-lg p-5 anim-fade-in-up">
        <h3 className="text-sm font-semibold text-[#00C2BB] uppercase tracking-wider mb-4">
          Cost projection
        </h3>
        <div className="flex justify-between items-center">
          <span className="text-gray-400">AWS Monthly Spend (pre-tax)</span>
          <span className="text-white font-semibold text-lg">
            {fallback != null ? `$${fallback.toLocaleString()}` : '—'}
          </span>
        </div>
      </div>
    )
  }
  const rows: Array<{ label: string; value: number; compare: boolean }> = [
    { label: 'AWS Infra Spend (excl. Marketplace)', value: run.aws_total, compare: false },
    { label: 'GCP On-Demand',              value: run.gcp_od,      compare: true },
    { label: 'GCP 1-Year CUD',             value: run.gcp_1yr_cud, compare: true },
    { label: 'GCP 3-Year CUD',             value: run.gcp_3yr_cud, compare: true },
  ]
  return (
    <div className="bg-white/[0.02] border border-white/10 rounded-lg p-5 anim-fade-in-up card-lift">
      <h3 className="text-sm font-semibold text-[#00C2BB] uppercase tracking-wider mb-4">
        Cost projection
      </h3>
      <div className="divide-y divide-white/5">
        {rows.map((r, i) => {
          const pct = r.compare ? pctVsAws(r.value, run.aws_total) : null
          return (
            <div key={r.label}
              className="flex flex-col sm:flex-row sm:items-center sm:justify-between py-2.5 gap-1 anim-fade-in-up"
              style={{ animationDelay: `${i * 70}ms` }}>
              <span className="text-gray-400 text-sm">{r.label}</span>
              <div className="flex items-baseline gap-3">
                <span className="font-semibold text-white tabular-nums">
                  {formatDollars(r.value)}
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

export function JobStatus({ user }: Readonly<{ user: UserInfo }>) {
  const { id } = useParams<{ id: string }>()
  const [job, setJob] = useState<Job | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [progress, setProgress] = useState<Progress | null>(null)
  const [runs, setRuns] = useState<RunResult[]>([])
  const [summary, setSummary] = useState<string | null>(null)
  const [refineOpen, setRefineOpen] = useState(false)
  const [instruction, setInstruction] = useState('')
  const [refining, setRefining] = useState(false)
  const [refineError, setRefineError] = useState<string | null>(null)
  const [retrying, setRetrying] = useState(false)
  const [retryError, setRetryError] = useState<string | null>(null)
  const [shareLink, setShareLink] = useState<string | null>(null)
  const [sharing, setSharing] = useState(false)
  const [shareError, setShareError] = useState<string | null>(null)

  const handleShare = async () => {
    if (!id) return
    setSharing(true)
    setShareError(null)
    try {
      const { url } = await shareJob(id)
      setShareLink(`${window.location.origin}${url}`)
    } catch (e: unknown) {
      setShareError(e instanceof Error ? e.message : String(e))
    } finally {
      setSharing(false)
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

  useEffect(() => {
    if (!id) return
    let cancelled = false
    let prevStatus: string | null = null
    const poll = async () => {
      if (cancelled) return
      const j = await getJob(id).catch(() => null)
      if (!j || cancelled) return
      setJob(j)
      setJobs(prev => prev.map(old => old.id === j.id ? { ...old, status: j.status } : old))
      if (j.status === 'running' || j.status === 'pending') {
        getProgress(id).then(p => { if (!cancelled && p) setProgress(p) })
        setTimeout(poll, 5000)
      } else if (j.status === 'done' && prevStatus !== 'done') {
        // First time we see done — fetch runs + summary.
        getRuns(id).then(r => { if (!cancelled) setRuns(r) })
        getSummary(id).then(s => { if (!cancelled) setSummary(s) })
      }
      prevStatus = j.status
    }
    poll()
    return () => { cancelled = true }
  }, [id, refining, retrying])

  if (!job) return <div className="min-h-screen bg-[#0a0a0f] text-white"><Nav user={user} /></div>

  return (
    <div className="min-h-screen bg-[#0a0a0f] text-white">
      <Nav user={user} />
      <div className="max-w-5xl mx-auto px-6 py-10 grid gap-8 lg:grid-cols-[1fr_320px]">
        <main className="flex flex-col gap-6">
          <h1 className="text-2xl font-semibold anim-fade-in-up">
            Report for <span className="text-[#645DF6]">{job.prospect}</span>
          </h1>

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
            </div>
          )}

          {job.status === 'done' && (
            <>
              <TotalsCard run={runs[0] ?? null} fallback={job.aws_spend} />

              <p className="text-xs text-gray-500 leading-relaxed -mt-2 anim-fade-in">
                AI-generated estimate — verify before sharing. If a mapping looks off,
                hit "Refine this report" below and tell the agent what to change.
              </p>

              {summary && (
                <Reveal>
                  <Summary markdown={summary} />
                </Reveal>
              )}

              <a href={downloadURL(job.id)}
                className="btn-shimmer w-full py-3 rounded-lg font-medium text-white text-center block
                  anim-fade-in-up delay-175">
                ↓ Download latest report
              </a>

              <Reveal delay={100}>
                <div className="bg-white/[0.02] border border-white/10 rounded-lg p-4 space-y-2">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <h3 className="text-sm font-semibold text-[#00C2BB] uppercase tracking-wider">
                        Share this report
                      </h3>
                      <p className="text-xs text-gray-500 mt-0.5">
                        Create a read-only public link, valid for 30 days.
                      </p>
                    </div>
                    <button
                      onClick={handleShare}
                      disabled={sharing}
                      className="text-xs font-medium text-[#645DF6] hover:text-[#8981ff] whitespace-nowrap
                        disabled:opacity-40 disabled:cursor-not-allowed transition-colors duration-150">
                      {sharing ? 'Creating…' : shareLink ? 'Regenerate link' : 'Create link'}
                    </button>
                  </div>
                  {shareError && <p className="text-xs text-orange-400">{shareError}</p>}
                  {shareLink && (
                    <input
                      readOnly
                      value={shareLink}
                      onFocus={e => e.currentTarget.select()}
                      className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-xs font-mono text-gray-300"
                    />
                  )}
                </div>
              </Reveal>

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
                    <p className="text-gray-400 text-xs mt-1 font-mono break-words">{job.error}</p>
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

          <Link to="/" className="nav-link text-xs text-[#645DF6] self-start">← New Estimation</Link>
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
    </div>
  )
}
