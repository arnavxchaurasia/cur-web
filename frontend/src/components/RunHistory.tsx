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

export function RunHistory({ jobId, runs }: Readonly<{ jobId: string; runs: RunResult[] }>) {
  const [expanded, setExpanded] = useState(false)
  if (runs.length <= 1) return null

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
        <ul className="divide-y divide-white/5 border-t border-white/10">
          {runs.map(run => (
            <li key={run.run_id} className="px-5 py-3 text-sm">
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                <div className="flex flex-col sm:flex-row sm:items-center gap-x-3 gap-y-1 min-w-0">
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
      )}
    </div>
  )
}
