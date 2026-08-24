import type { Job } from '../api/jobs'
import { useNavigate } from 'react-router-dom'

const statusColor: Record<string, string> = {
  pending: 'text-gray-400',
  running: 'text-[#00C2BB]',
  done:    'text-[#00C2BB]',
  failed:  'text-orange-400',
}

function StatusBadge({ status }: Readonly<{ status: string }>) {
  if (status === 'running') {
    return (
      <span className="flex items-center gap-1.5 text-[#00C2BB]">
        <span className="relative flex w-2 h-2">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#00C2BB] opacity-60" />
          <span className="relative inline-flex rounded-full w-2 h-2 bg-[#00C2BB]" />
        </span>{' '}
        Running
      </span>
    )
  }
  const icon: Record<string, string> = { done: '✓', failed: '✕', pending: '–' }
  const label = status.charAt(0).toUpperCase() + status.slice(1)
  return (
    <span className={`flex items-center gap-1 ${statusColor[status] ?? 'text-gray-400'}`}>
      <span className="text-xs font-bold">{icon[status] ?? ''}</span>
      {label}
    </span>
  )
}

export function JobList({ jobs }: Readonly<{ jobs: Job[] }>) {
  const nav = useNavigate()
  if (!jobs.length) return <p className="text-sm text-gray-500">No reports yet</p>
  return (
    <ul className="divide-y divide-white/10 text-sm">
      {jobs.map((j, i) => (
        <li
          key={j.id}
          role="button"
          tabIndex={0}
          className="flex justify-between items-center py-2 cursor-pointer
            hover:bg-white/[0.04] px-1 rounded transition-colors duration-150
            anim-fade-in-up"
          style={{ animationDelay: `${i * 55}ms` }}
          onClick={() => nav(`/jobs/${j.id}`)}
          onKeyDown={e => { if (e.key === 'Enter') nav(`/jobs/${j.id}`) }}
        >
          <span className="text-gray-200 truncate max-w-[140px]">{j.prospect}</span>
          <StatusBadge status={j.status} />
        </li>
      ))}
    </ul>
  )
}
