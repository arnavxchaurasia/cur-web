import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Login } from './pages/Login'
import { Upload } from './pages/Upload'
import { JobStatus } from './pages/JobStatus'
import { AdminJobs } from './pages/AdminJobs'
import { useEffect, useState } from 'react'
import { getMe } from './api/auth'
import type { UserInfo } from './api/auth'

export default function App() {
  const [user, setUser] = useState<UserInfo | null | undefined>(undefined)

  useEffect(() => { getMe().then(setUser).catch(() => setUser(null)) }, [])

  if (user === undefined) return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#0a0a0f' }}>
      <div style={{ width: 32, height: 32, border: '3px solid #645DF6', borderTopColor: 'transparent', borderRadius: '50%', animation: 'spin 0.7s linear infinite' }} />
      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
    </div>
  )

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={user ? <Navigate to="/" /> : <Login />} />
        <Route path="/" element={user ? <Upload user={user} /> : <Navigate to="/login" />} />
        <Route path="/jobs/:id" element={user ? <JobStatus user={user} /> : <Navigate to="/login" />} />
        <Route path="/admin" element={user?.is_admin ? <AdminJobs user={user} /> : <Navigate to="/" />} />
        <Route path="*" element={<Navigate to={user ? "/" : "/login"} replace />} />
      </Routes>
    </BrowserRouter>
  )
}
