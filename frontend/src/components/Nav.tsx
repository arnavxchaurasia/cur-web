import facetsFIcon from '../assets/facets-logo-f.svg'
import type { UserInfo } from '../api/auth'
import { Link, useLocation } from 'react-router-dom'
import { useState, useRef, useEffect } from 'react'

export function Nav({ user }: Readonly<{ user: UserInfo }>) {
  const location = useLocation()
  const isAdmin = Boolean(user?.is_admin)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const settingsRef = useRef<HTMLDivElement>(null)
  const mobileRef = useRef<HTMLDivElement>(null)

  const navItemClass = (path: string, exact = true) => {
    const isActive = exact ? location.pathname === path : location.pathname.startsWith(path)
    return isActive
      ? 'nav-link text-[#00C2BB] font-medium'
      : 'nav-link text-gray-400 hover:text-white transition-colors duration-150'
  }

  const isSettingsActive = [
    '/admin/analytics',
    '/admin/audit',
    '/admin/assumptions',
    '/admin/report-settings',
  ].some(p => location.pathname === p) || (location.pathname === '/admin' && location.search.includes('tab=shares'))

  // Close dropdown on outside click
  useEffect(() => {
    if (!settingsOpen) return
    const handleClickOutside = (e: MouseEvent | TouchEvent) => {
      if (settingsRef.current && !settingsRef.current.contains(e.target as Node)) {
        setSettingsOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    document.addEventListener('touchstart', handleClickOutside)
    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
      document.removeEventListener('touchstart', handleClickOutside)
    }
  }, [settingsOpen])

  // Close mobile drawer on route change or click outside
  useEffect(() => {
    setMobileMenuOpen(false)
    setSettingsOpen(false)
  }, [location.pathname, location.search])

  useEffect(() => {
    if (!mobileMenuOpen) return
    const handleClickOutside = (e: MouseEvent | TouchEvent) => {
      if (mobileRef.current && !mobileRef.current.contains(e.target as Node)) {
        setMobileMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    document.addEventListener('touchstart', handleClickOutside)
    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
      document.removeEventListener('touchstart', handleClickOutside)
    }
  }, [mobileMenuOpen])

  return (
    <nav className="flex items-center justify-between px-6 py-3 border-b border-white/10 anim-slide-down backdrop-blur-md bg-[#0a0a0f]/85 sticky top-0 z-30">
      <div className="flex items-center gap-3">
        <Link to="/" className="flex items-center gap-3 transition-opacity duration-150 hover:opacity-85">
          <img src={facetsFIcon} alt="Facets" className="h-5" />
          <span className="hidden sm:inline text-white/30">|</span>
          <span className="text-sm font-semibold tracking-tight">
            <span className="hidden sm:inline">AWS → GCP Cost Estimator</span>
            <span className="sm:hidden">Estimator</span>
          </span>
        </Link>
      </div>

      <div className="flex items-center gap-4 text-sm text-gray-400">
        {/* Desktop Nav */}
        <div className="hidden sm:flex items-center gap-4">
          {isAdmin && (
            <Link to="/admin" className={navItemClass('/admin')}>All Reports</Link>
          )}
          {isAdmin && (
            <>
              <Link to="/admin/users" className={navItemClass('/admin/users')}>Users</Link>
              <Link to="/admin/catalog" className={navItemClass('/admin/catalog')}>Catalog</Link>

              {/* Settings Dropdown */}
              <div className="relative" ref={settingsRef}>
                <button
                  onClick={() => setSettingsOpen(v => !v)}
                  className={`flex items-center gap-1.5 py-1 px-2 rounded-lg hover:bg-white/5 transition-colors cursor-pointer ${
                    isSettingsActive ? 'text-[#00C2BB] font-medium' : 'text-gray-400 hover:text-white'
                  }`}
                  aria-haspopup="true"
                  aria-expanded={settingsOpen}
                >
                  <span>Settings</span>
                  <svg
                    className={`h-3 w-3 text-gray-500 transition-transform duration-150 ${settingsOpen ? 'rotate-180' : ''}`}
                    viewBox="0 0 12 12"
                    fill="none"
                  >
                    <path d="M3 4.5 6 7.5 9 4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>

                {settingsOpen && (
                  <div className="absolute right-0 mt-2 w-52 z-50 rounded-lg border border-white/10 bg-[#13131a] shadow-2xl py-1 overflow-hidden anim-scale-in text-xs">
                    <Link
                      to="/admin/analytics"
                      onClick={() => setSettingsOpen(false)}
                      className="flex items-center gap-2 px-4 py-2 text-gray-300 hover:bg-white/5 hover:text-white transition-colors"
                    >
                      <span>📈</span>
                      <span>Analytics</span>
                    </Link>
                    <Link
                      to="/admin/audit"
                      onClick={() => setSettingsOpen(false)}
                      className="flex items-center gap-2 px-4 py-2 text-gray-300 hover:bg-white/5 hover:text-white transition-colors"
                    >
                      <span>📋</span>
                      <span>Audit</span>
                    </Link>
                    <Link
                      to="/admin/assumptions"
                      onClick={() => setSettingsOpen(false)}
                      className="flex items-center gap-2 px-4 py-2 text-gray-300 hover:bg-white/5 hover:text-white transition-colors"
                    >
                      <span>⚙️</span>
                      <span>Assumptions</span>
                    </Link>
                    <Link
                      to="/admin/report-settings"
                      onClick={() => setSettingsOpen(false)}
                      className="flex items-center gap-2 px-4 py-2 text-gray-300 hover:bg-white/5 hover:text-white transition-colors"
                    >
                      <span>📑</span>
                      <span>Report Settings</span>
                    </Link>
                    <Link
                      to="/admin/integrations"
                      onClick={() => setSettingsOpen(false)}
                      className="flex items-center gap-2 px-4 py-2 text-gray-300 hover:bg-white/5 hover:text-white transition-colors"
                    >
                      <span>🔔</span>
                      <span>Integrations</span>
                    </Link>
                    <Link
                      to="/admin/tickets"
                      onClick={() => setSettingsOpen(false)}
                      className="flex items-center gap-2 px-4 py-2 text-gray-300 hover:bg-white/5 hover:text-white transition-colors"
                    >
                      <span>🎫</span>
                      <span>Support Tickets</span>
                    </Link>
                    <Link
                      to="/admin?tab=shares"
                      onClick={() => setSettingsOpen(false)}
                      className="flex items-center gap-2 px-4 py-2 text-gray-300 hover:bg-white/5 hover:text-white transition-colors"
                    >
                      <span>🔗</span>
                      <span>Shares</span>
                    </Link>
                  </div>
                )}
              </div>
            </>
          )}
        </div>

        {/* Mobile Hamburger / Menu Button */}
        <div className="sm:hidden flex items-center gap-2 text-xs" ref={mobileRef}>
          <button
            onClick={() => setMobileMenuOpen(v => !v)}
            className="p-1.5 rounded-lg border border-white/10 text-gray-300 hover:text-white hover:bg-white/5 cursor-pointer"
            aria-label="Toggle Navigation Menu"
            aria-expanded={mobileMenuOpen}
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              {mobileMenuOpen ? (
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              ) : (
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
              )}
            </svg>
          </button>

          {mobileMenuOpen && (
            <div className="absolute top-full left-0 right-0 bg-[#13131a] border-b border-white/10 p-4 shadow-2xl flex flex-col gap-2 anim-slide-down">
              <Link to="/" onClick={() => setMobileMenuOpen(false)} className="text-gray-300 hover:text-white py-1">
                Upload New CUR
              </Link>
              {isAdmin && (
                <>
                  <Link to="/admin" onClick={() => setMobileMenuOpen(false)} className="text-gray-300 hover:text-white py-1 font-medium">
                    All Reports
                  </Link>
                  <Link to="/admin/users" onClick={() => setMobileMenuOpen(false)} className="text-gray-300 hover:text-white py-1">
                    Users
                  </Link>
                  <Link to="/admin/catalog" onClick={() => setMobileMenuOpen(false)} className="text-gray-300 hover:text-white py-1">
                    Catalog
                  </Link>
                  <div className="pt-2 border-t border-white/10">
                    <p className="text-[11px] uppercase tracking-wider text-gray-500 mb-1">Settings &amp; Tools</p>
                    <Link to="/admin/analytics" onClick={() => setMobileMenuOpen(false)} className="block py-1 text-gray-300 hover:text-white">
                      Analytics
                    </Link>
                    <Link to="/admin/audit" onClick={() => setMobileMenuOpen(false)} className="block py-1 text-gray-300 hover:text-white">
                      Audit
                    </Link>
                    <Link to="/admin/assumptions" onClick={() => setMobileMenuOpen(false)} className="block py-1 text-gray-300 hover:text-white">
                      Assumptions
                    </Link>
                    <Link to="/admin/report-settings" onClick={() => setMobileMenuOpen(false)} className="block py-1 text-gray-300 hover:text-white">
                      Report Settings
                    </Link>
                    <Link to="/admin/tickets" onClick={() => setMobileMenuOpen(false)} className="block py-1 text-gray-300 hover:text-white">
                      Support Tickets
                    </Link>
                    <Link to="/admin?tab=shares" onClick={() => setMobileMenuOpen(false)} className="block py-1 text-gray-300 hover:text-white">
                      Shares
                    </Link>
                  </div>
                </>
              )} 
            </div>
          )}
        </div>

        <UserMenu user={user} />
      </div>
    </nav>
  )
}

export function UserMenu({ user }: Readonly<{ user: UserInfo }>) {
  const [open, setOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const initial = (user.name || user.email || '?').trim().charAt(0).toUpperCase()

  useEffect(() => {
    if (!open) return
    const handleClickOutside = (e: MouseEvent | TouchEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    document.addEventListener('touchstart', handleClickOutside)
    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
      document.removeEventListener('touchstart', handleClickOutside)
    }
  }, [open])

  return (
    <div className="relative" ref={menuRef}>
      <button
        onClick={() => setOpen(v => !v)}
        className="flex items-center gap-2 rounded-full p-0.5 pr-2 hover:bg-white/5 transition-colors duration-150 cursor-pointer"
        aria-haspopup="true"
        aria-expanded={open}
      >
        {user.picture ? (
          <img
            src={user.picture}
            alt=""
            referrerPolicy="no-referrer"
            className="h-8 w-8 rounded-full object-cover"
          />
        ) : (
          <span className="h-8 w-8 rounded-full bg-[#645DF6] text-white text-sm font-semibold flex items-center justify-center">
            {initial}
          </span>
        )}
        <span className="hidden sm:inline text-gray-300 text-sm max-w-[160px] truncate">
          {user.name || user.email}
        </span>
        <svg className={`hidden sm:block h-3 w-3 text-gray-500 transition-transform duration-150 ${open ? 'rotate-180' : ''}`} viewBox="0 0 12 12" fill="none">
          <path d="M3 4.5 6 7.5 9 4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && (
        <div className="absolute right-0 mt-2 w-72 z-50 rounded-lg border border-white/10 bg-[#13131a] shadow-2xl overflow-hidden anim-scale-in">
          <div className="px-4 py-3 border-b border-white/10">
            <p className="text-sm font-medium text-white truncate">{user.name || 'Signed in'}</p>
            <p className="text-xs text-gray-400 truncate">{user.email}</p>
            {user.is_facets_employee && (
              <span className="inline-block mt-1.5 px-2 py-0.5 text-[10px] uppercase font-semibold tracking-wider bg-[#00C2BB]/15 text-[#00C2BB] border border-[#00C2BB]/30 rounded">
                Facets Team Admin
              </span>
            )}
          </div>

          {/* 1. Profile first */}
          <div className="py-1 border-b border-white/10">
            <Link
              to="/profile"
              onClick={() => setOpen(false)}
              className="w-full text-left px-4 py-2 text-xs text-gray-300 hover:bg-white/5 hover:text-white transition-colors flex items-center justify-between"
            >
              <span className="flex items-center gap-2">
                <span>👤</span>
                <span className="font-medium text-white">Profile &amp; Settings</span>
              </span>
              <span className="text-[10px] text-gray-500">Edit</span>
            </Link>
          </div>

          {/* API Tokens */}
          <div className="py-2.5 px-4 border-b border-white/10 space-y-1.5">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
                <span>🔑</span> API Tokens
              </span>
            </div>
            <p className="text-[10px] text-gray-500 leading-tight">
              Personal access tokens authenticate REST API calls (e.g.{' '}
              <code className="font-mono">GET /api/jobs</code>). Manage them on
              the API Tokens page.
            </p>
            <Link
              to="/profile/tokens"
              onClick={() => setOpen(false)}
              className="w-full text-left px-4 py-2 text-xs text-[#00C2BB] hover:bg-white/5 hover:text-white transition-colors flex items-center justify-between"
            >
              <span>Manage API Tokens →</span>
            </Link>
          </div>

          {/* 3. Currency Preferences Route Link */}

          {user.is_facets_employee && (
            <div className="py-1 border-b border-white/10 text-xs">
              <Link
                to="/admin"
                onClick={() => setOpen(false)}
                className="flex items-center gap-2 px-4 py-2 text-gray-300 hover:bg-white/5 hover:text-white transition-colors"
              >
                <span>📊</span>
                <span>All Reports (Admin)</span>
              </Link>
            </div>
          )}

          {/* Sign out */}
          <div className="py-1">
            <button
              onClick={async () => {
                setOpen(false)
                await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' })
                window.location.replace('/login')
              }}
              className="w-full text-left px-4 py-2 text-xs text-red-400 hover:bg-red-500/10 transition-colors cursor-pointer"
            >
              Sign Out
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
