import { useState } from 'react'
import type { RunResult } from '../api/jobs'
import { downloadURL } from '../api/jobs'

function formatTs(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const yyyy = d.getUTCFullYear()
  const mm = String(d.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(d.getUTCDate()).padStart(2, '0')
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mi = String(d.getUTCMinutes()).padStart(2, '0')
  return `${yyyy}-${mm}-${dd} ${hh}:${mi} UTC`
}

function money(n: number | null | undefined): string {
  if (n == null) return '—'
  return `$${Math.round(n).toLocaleString()}`
}

function InstructionLine({ text }: Readonly<{ text: string }>) {
  const [open, setOpen] = useState(false)
  const truncated = text.length > 80
  const display = open || !truncated ? text : text.slice(0, 80) + '…'
  return (
    <p className="text-xs text-gray-500 mt-1 pl-2 border-l border-white/10">
      <span className="text-gray-400">“</span>{display}<span className="text-gray-400">”</span>
      {truncated && (
        <button
          onClick={() => setOpen(o => !o)}
          className="ml-2 text-[#645DF6] hover:underline">
          {open ? 'less' : 'more'}
        </button>
      )}
    </p>
  )
}

function diffCell(label: string, a: number, b: number) {
  const delta = b - a
  const pct = a !== 0 ? (delta / a) * 100 : 0
  const sign = delta > 0 ? '+' : ''
  return (
    <div key={label} className="flex items-center justify-between text-xs py-1.5">
      <span className="text-gray-500">{label}</span>
      <span className="flex items-center gap-2 tabular-nums">
        <span className="text-gray-400">{money(a)}</span>
        <span className="text-gray-600">→</span>
        <span className="text-gray-200">{money(b)}</span>
        <span className={delta === 0 ? 'text-gray-500' : delta > 0 ? 'text-orange-400' : 'text-[#00C2BB]'}>
          ({sign}{pct.toFixed(1)}%)
        </span>
      </span>
    </div>
  )
}

function CompareView({ a, b }: Readonly<{ a: RunResult; b: RunResult }>) {
  return (
    <div className="mx-5 mb-4 border border-white/10 rounded-lg p-4 bg-white/[0.02] anim-scale-in">
      <p className="text-xs uppercase tracking-wider text-gray-500 mb-2">
        Comparing {formatTs(a.ts_utc)} vs {formatTs(b.ts_utc)}
      </p>
      <div className="divide-y divide-white/5">
        {diffCell('AWS total', a.aws_total, b.aws_total)}
        {diffCell('GCP on-demand', a.gcp_od, b.gcp_od)}
        {diffCell('GCP 1yr CUD', a.gcp_1yr_cud, b.gcp_1yr_cud)}
        {diffCell('GCP 3yr CUD', a.gcp_3yr_cud, b.gcp_3yr_cud)}
      </div>
      {b.instruction && (
        <p className="text-xs text-gray-500 mt-2">
          Instruction for later run: <span className="text-gray-300">"{b.instruction}"</span>
        </p>
      )}
    </div>
  )
}

export function RunHistory({ jobId, runs }: Readonly<{ jobId: string; runs: RunResult[] }>) {
  const [expanded, setExpanded] = useState(false)
  const [compareMode, setCompareMode] = useState(false)
  const [selected, setSelected] = useState<string[]>([])
  if (runs.length <= 1) return null

  const toggleSelect = (runId: string) => {
    setSelected(prev => {
      if (prev.includes(runId)) return prev.filter(id => id !== runId)
      if (prev.length >= 2) return [prev[1], runId]
      return [...prev, runId]
    })
  }

  // Order chronologically (oldest first) for a sensible "before → after" diff,
  // regardless of the order the two runs were clicked in.
  const selectedRuns = runs
    .filter(r => selected.includes(r.run_id))
    .sort((r1, r2) => r1.ts_utc.localeCompare(r2.ts_utc))

  return (
    <div className="bg-white/[0.02] border border-white/10 rounded-lg overflow-hidden">
      <button
        onClick={() => setExpanded(e => !e)}
        className="w-full flex items-center justify-between px-5 py-3 text-left
          hover:bg-white/[0.03] transition">
        <div>
          <h3 className="text-sm font-semibold text-[#00C2BB] uppercase tracking-wider">
            Run history
          </h3>
          <p className="text-xs text-gray-500 mt-0.5">{runs.length} versions</p>
        </div>
        <span className={`text-[#645DF6] transition-transform ${expanded ? 'rotate-90' : ''}`}>
          ▶
        </span>
      </button>
      {expanded && (
        <>
          <div className="px-5 pb-2 flex items-center justify-between border-t border-white/10 pt-3">
            <button
              onClick={() => { setCompareMode(m => !m); setSelected([]) }}
              className={`text-xs font-medium transition-colors duration-150 ${
                compareMode ? 'text-[#645DF6]' : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              {compareMode ? '✕ Cancel compare' : 'Compare two runs'}
            </button>
            {compareMode && (
              <span className="text-xs text-gray-500">{selected.length}/2 selected</span>
            )}
          </div>

          {compareMode && selectedRuns.length === 2 && (
            <CompareView a={selectedRuns[0]} b={selectedRuns[1]} />
          )}

          <ul className="divide-y divide-white/5 border-t border-white/10">
            {runs.map(run => (
              <li key={run.run_id} className="px-5 py-3 text-sm">
                <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                  <div className="flex flex-col sm:flex-row sm:items-center gap-x-3 gap-y-1 min-w-0">
                    {compareMode && (
                      <input
                        type="checkbox"
                        checked={selected.includes(run.run_id)}
                        onChange={() => toggleSelect(run.run_id)}
                        className="accent-[#645DF6]"
                      />
                    )}
                    <span className="text-gray-300 whitespace-nowrap font-mono text-xs">
                      {formatTs(run.ts_utc)}
                    </span>
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium w-fit ${
                      run.run_type === 'refinement'
                        ? 'bg-[#645DF6]/20 text-[#645DF6]'
                        : 'bg-[#00C2BB]/20 text-[#00C2BB]'
                    }`}>
                      {run.run_type.charAt(0).toUpperCase() + run.run_type.slice(1)}
                    </span>
                    <span className="text-gray-400 text-xs truncate">
                      AWS {money(run.aws_total)} → GCP {money(run.gcp_od)}{' '}
                      <span className="text-gray-500">(3yr {money(run.gcp_3yr_cud)})</span>
                    </span>
                  </div>
                  <a
                    href={downloadURL(jobId, run.run_id)}
                    className="text-xs text-[#645DF6] hover:text-[#7d77f8] hover:underline whitespace-nowrap self-start sm:self-auto">
                    ↓ download
                  </a>
                </div>
                {run.run_type === 'refinement' && run.instruction && (
                  <InstructionLine text={run.instruction} />
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}
