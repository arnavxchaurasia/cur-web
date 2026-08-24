import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Nav } from '../components/Nav'
import { DropZone } from '../components/DropZone'
import { JobList } from '../components/JobList'
import { ContactCard } from '../components/ContactCard'
import { Reveal } from '../components/Reveal'
import { createJob, listJobs } from '../api/jobs'
import type { Job } from '../api/jobs'
import type { UserInfo } from '../api/auth'
import { PHASES, TOTAL_PHASES } from '../lib/phases'
import { useTitle } from '../lib/useTitle'

export function Upload({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('New Estimation')
  const [prospect, setProspect] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [jobs, setJobs] = useState<Job[]>([])
  const nav = useNavigate()

  useEffect(() => { listJobs().then(setJobs) }, [])

  const submit = async () => {
    if (!file || !prospect.trim()) return
    setSubmitting(true)
    try {
      const { id } = await createJob(file, prospect.trim())
      nav(`/jobs/${id}`)
    } finally {
      setSubmitting(false)
    }
  }

  const ready = !!file && !!prospect.trim() && !submitting

  return (
    <div className="min-h-screen bg-[#0a0a0f] text-white">
      <Nav user={user} />
      <div className="max-w-5xl mx-auto px-6 py-10 grid gap-8 lg:grid-cols-[1fr_320px]">
        <main className="flex flex-col gap-6">
          <div className="anim-fade-in-up">
            <h1 className="text-2xl font-semibold">New Estimation</h1>
            <p className="text-sm text-gray-500 mt-1">
              An AI agent maps each AWS line item to its GCP equivalent and produces a per-row cost projection.
            </p>
          </div>
          <div className="flex flex-col gap-2 anim-fade-in-up delay-100">
            <label htmlFor="prospect-name" className="text-xs uppercase tracking-wider text-gray-400">Prospect / Customer Name</label>
            <input id="prospect-name" value={prospect} onChange={e => setProspect(e.target.value)}
              placeholder="e.g. Acme Corp"
              className="bg-white/5 border border-white/20 rounded-lg px-4 py-2 text-sm outline-none
                focus:border-[#645DF6] transition-colors duration-150" />
          </div>
          <div className="flex flex-col gap-2 anim-fade-in-up delay-175">
            <label htmlFor="bill-upload" className="text-xs uppercase tracking-wider text-gray-400">AWS bill — CUR export, Cost Explorer CSV, Parquet, ZIP, or console PDF</label>
            <DropZone file={file} onChange={setFile} inputId="bill-upload" />
          </div>
          <div className="anim-fade-in-up delay-250">
            <button onClick={submit} disabled={!ready}
              className={`w-full py-3 rounded-lg font-medium text-white transition-all duration-200
                ${ready ? 'btn-shimmer anim-glow-breathe cursor-pointer' : 'bg-gradient-to-r from-[#645DF6] to-[#00C2BB] opacity-40 cursor-not-allowed'}`}>
              {submitting ? 'Uploading…' : 'Submit'}
            </button>
          </div>

          <Reveal className="mt-4">
            <section className="grid gap-6 md:grid-cols-2">
              <div className="bg-white/[0.02] border border-white/10 rounded-lg p-5 card-lift">
                <h2 className="text-sm font-semibold text-[#00C2BB] mb-3 uppercase tracking-wider">What you'll get</h2>
                <ul className="text-sm text-gray-300 space-y-2 list-disc list-inside marker:text-[#645DF6]">
                  <li>Per-line-item AWS → GCP service mapping</li>
                  <li>On-demand, 1-year and 3-year CUD pricing</li>
                  <li>Region-aware rates with methodology notes</li>
                  <li>Customer-shareable HTML report you can hand off as-is</li>
                </ul>
              </div>
              <div className="bg-white/[0.02] border border-white/10 rounded-lg p-5 card-lift">
                <h2 className="text-sm font-semibold text-[#645DF6] mb-3 uppercase tracking-wider">How it runs</h2>
                <p className="text-sm text-gray-300 mb-3">
                  An AI agent works through {TOTAL_PHASES} phases on your bill — typically 10–30 minutes total.
                </p>
                <ol className="text-xs text-gray-400 space-y-1.5">
                  {PHASES.map(p => (
                    <li key={p.n}>
                      <span className="text-gray-200 font-mono mr-2">{p.n}.</span>{p.name} — {p.blurb}
                    </li>
                  ))}
                </ol>
              </div>
            </section>
          </Reveal>

          <Reveal delay={80}>
            <p className="text-xs text-gray-500 leading-relaxed">
              Accepted: AWS Cost &amp; Usage Report (CUR, often a ZIP of CSV/Parquet), Cost Explorer "Cost &amp; Usage Detail" CSV export, or an AWS console PDF bill. CSV/CUR give the cleanest projection; PDFs work but lose some classifier signal.
              Your file stays on this server — no upload to third parties. We email you when the report is ready.
            </p>
          </Reveal>
        </main>
        <aside className="lg:border-l lg:border-white/10 lg:pl-8 flex flex-col gap-6">
          <div className="anim-fade-in delay-250">
            <p className="text-xs uppercase tracking-wider text-gray-400 mb-3">Your Reports</p>
            <JobList jobs={jobs} />
          </div>
          <div className="anim-fade-in delay-325">
            <ContactCard />
          </div>
        </aside>
      </div>
    </div>
  )
}
