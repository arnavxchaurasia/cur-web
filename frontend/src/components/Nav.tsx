import facetsFIcon from '../assets/facets-logo-f.svg'
import type { UserInfo } from '../api/auth'
import { logoutURL } from '../api/auth'
import { Link } from 'react-router-dom'

function handleSignOut(e: React.MouseEvent) {
  e.preventDefault()
  fetch(logoutURL(), { method: 'POST', credentials: 'same-origin' })
    .finally(() => { window.location.href = '/' })
}

export function Nav({ user }: Readonly<{ user: UserInfo }>) {

  return (
    <nav className="flex items-center justify-between px-6 py-3 border-b border-white/10 anim-slide-down backdrop-blur-sm bg-[#0a0a0f]/80 sticky top-0 z-30">
      <Link to="/" className="flex items-center gap-3 transition-opacity duration-150 hover:opacity-85">
        <img src={facetsFIcon} alt="Facets" className="h-5" />
        <span className="hidden sm:inline text-white/30">|</span>
        <span className="text-sm font-semibold tracking-tight">
          <span className="hidden sm:inline">AWS → GCP Cost Estimator</span>
          <span className="sm:hidden">Estimator</span>
        </span>
      </Link>
      <div className="flex items-center gap-4 text-sm text-gray-400">
        {user.is_admin && (
          <Link to="/admin" className="nav-link text-[#00C2BB]">All Reports</Link>
        )}
        <span className="hidden sm:inline">{user.email}</span>
        <button onClick={handleSignOut} className="nav-link text-[#645DF6]">Sign Out</button>
      </div>
    </nav>
  )
}
