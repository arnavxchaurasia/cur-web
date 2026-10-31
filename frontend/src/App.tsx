import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Login } from './pages/Login'
import { Upload } from './pages/Upload'
import { JobStatus } from './pages/JobStatus'
import { AdminJobs } from './pages/AdminJobs'
import { AdminUsers } from './pages/AdminUsers'
import { AdminCatalog } from './pages/AdminCatalog'
import { AdminAssumptions } from './pages/AdminAssumptions'
import { AdminReportSettings } from './pages/AdminReportSettings'
import { AdminAudit } from './pages/AdminAudit'
import { AdminAnalytics } from './pages/AdminAnalytics'
import { AdminNotifications } from './pages/AdminNotifications'
import { SharedReport } from './pages/SharedReport'
import { Tickets } from './pages/Tickets'
import { Profile } from './pages/Profile'
import { ApiTokens } from './pages/ApiTokens'
import { Bills } from './pages/Bills'
import { ReleaseDashboard } from './pages/ReleaseDashboard'
import { useEffect, useState } from 'react'
import { getMe } from './api/auth'
import type { UserInfo } from './api/auth'
import { Toaster } from 'sonner'
import { ThemeProvider } from './lib/themeContext'

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

  const isAdmin = Boolean(user?.is_admin)

  return (
    <ThemeProvider>
        <BrowserRouter>
          <Toaster position="top-center" richColors closeButton theme="dark" duration={3500} toastOptions={{ duration: 3500 }} visibleToasts={3} />
        <Routes>
          <Route path="/login" element={user ? <Navigate to="/" /> : <Login />} />
          <Route path="/" element={user ? <Upload user={user} /> : <Navigate to="/login" />} />
          <Route path="/releases" element={user ? <ReleaseDashboard user={user} /> : <Navigate to="/login" />} />
          <Route path="/jobs/:id" element={user ? <JobStatus user={user} /> : <Navigate to="/login" />} />
          <Route path="/admin" element={user && isAdmin ? <AdminJobs user={user} /> : <Navigate to="/" />} />
          <Route path="/admin/users" element={user && isAdmin ? <AdminUsers user={user} /> : <Navigate to="/" />} />
          <Route path="/admin/catalog" element={user && isAdmin ? <AdminCatalog user={user} /> : <Navigate to="/" />} />
          <Route path="/admin/assumptions" element={user && isAdmin ? <AdminAssumptions user={user} /> : <Navigate to="/" />} />
          <Route path="/admin/report-settings" element={user && isAdmin ? <AdminReportSettings user={user} /> : <Navigate to="/" />} />
          <Route path="/admin/audit" element={user && isAdmin ? <AdminAudit user={user} /> : <Navigate to="/" />} />
          <Route path="/admin/analytics" element={user && isAdmin ? <AdminAnalytics user={user} /> : <Navigate to="/" />} />
          <Route path="/admin/notifications" element={user && isAdmin ? <AdminNotifications user={user} /> : <Navigate to="/" />} />
          <Route path="/admin/tickets" element={user && isAdmin ? <Tickets user={user} /> : <Navigate to="/" />} />
        <Route path="/admin/bills" element={user && isAdmin ? <Bills user={user} /> : <Navigate to="/" />} />
        <Route path="/profile" element={user ? <Profile user={user} /> : <Navigate to="/login" />} />
        <Route path="/profile/tokens" element={user ? <ApiTokens user={user} /> : <Navigate to="/login" />} />
          <Route path="/reports/:token" element={<SharedReport />} />
          <Route path="*" element={<Navigate to={user ? "/" : "/login"} replace />} />
        </Routes>
      </BrowserRouter>
  </ThemeProvider>
  )
}
