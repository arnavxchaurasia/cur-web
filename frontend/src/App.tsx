import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Login } from './pages/Login'
import { Upload } from './pages/Upload'
import { JobStatus } from './pages/JobStatus'
import { AdminJobs } from './pages/AdminJobs'
import { AdminUsers } from './pages/AdminUsers'
import { AdminCatalog } from './pages/AdminCatalog'
import { AdminAudit } from './pages/AdminAudit'
import { AdminAnalytics } from './pages/AdminAnalytics'
import { SharedReport } from './pages/SharedReport'
import { Facets } from './pages/Facets'
import { useEffect, useState } from 'react'
import { getMe } from './api/auth'
import type { UserInfo } from './api/auth'

export default function App() {
  const [user, setUser] = useState<UserInfo | null | undefined>(undefined)

  useEffect(() => { getMe().then(setUser).catch(() => setUser(null)) }, [])

  // /reports/:token is a public, unauthenticated page — don't block it behind
  // the getMe() loading spinner or force a login redirect.
  if (window.location.pathname.startsWith('/reports/')) {
    return (
      <BrowserRouter>
        <Routes>
          <Route path="/reports/:token" element={<SharedReport />} />
        </Routes>
      </BrowserRouter>
    )
  }

  if (user === undefined) return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#0a0a0f' }}>
      <div style={{ width: 32, height: 32, border: '3px solid #645DF6', borderTopColor: 'transparent', borderRadius: '50%', animation: 'spin 0.7s linear infinite' }} />
      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
    </div>
  )

  const isFacetsEmployee = !!user?.email.toLowerCase().endsWith('@facets.cloud')

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={user ? <Navigate to="/" /> : <Login />} />
        <Route path="/" element={user ? <Upload user={user} /> : <Navigate to="/login" />} />
        <Route path="/jobs/:id" element={user ? <JobStatus user={user} /> : <Navigate to="/login" />} />
        <Route path="/admin" element={user?.is_admin ? <AdminJobs user={user} /> : <Navigate to="/" />} />
        <Route path="/admin/users" element={user?.is_admin ? <AdminUsers user={user} /> : <Navigate to="/" />} />
        <Route path="/admin/catalog" element={user?.is_admin ? <AdminCatalog user={user} /> : <Navigate to="/" />} />
        <Route path="/admin/audit" element={user?.is_admin ? <AdminAudit user={user} /> : <Navigate to="/" />} />
        <Route path="/admin/analytics" element={user?.is_admin ? <AdminAnalytics user={user} /> : <Navigate to="/" />} />
        <Route path="/reports/:token" element={<SharedReport />} />
        {/* Not linked from any customer-facing nav. Reached via the small
            marker Nav renders next to the logo, itself shown only for
            @facets.cloud sessions. The real gate is server-side (every
            /api/facets/* handler re-checks auth.Session.IsFacetsEmployee);
            this client check just keeps a non-Facets user redirected here
            some other way from seeing anything. */}
        <Route path="/facets" element={isFacetsEmployee && user ? <Facets user={user} /> : <Navigate to="/" />} />
        <Route path="*" element={<Navigate to={user ? "/" : "/login"} replace />} />
      </Routes>
    </BrowserRouter>
  )
}
