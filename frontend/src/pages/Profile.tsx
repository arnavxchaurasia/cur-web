import { useEffect, useState } from 'react'
import { Layout } from '../components/Layout'
import { getUserProfile, saveUserProfile, type UserProfile } from '../api/auth'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'
import { toast } from 'sonner'
import { BackButton } from '../components/BackButton'

export function Profile({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Profile')
  const [profile, setProfile] = useState<UserProfile | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const p = await getUserProfile()
      setProfile(p)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to load profile')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!profile) return
    setSaving(true)
    setError(null)
    try {
      await saveUserProfile(profile)
      toast.success('Profile saved')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to save profile')
    } finally {
      setSaving(false)
    }
  }

  if (loading || !profile) {
    return (
      <Layout user={user}>
        <div className="max-w-2xl mx-auto px-6 py-10 text-center">
          <div className="flex items-center justify-center h-32">
            <div className="flex gap-2">
              {[0, 1, 2, 3].map((i, idx) => (
                <div key={i} className="w-2 h-2 rounded-full bg-[#00C2BB] animate-pulse" style={{ animationDelay: `${idx * 100}ms` }} />
              ))}
            </div>
          </div>
          <p className="text-sm text-gray-500 mt-2">Loading profile…</p>
        </div>
      </Layout>
    )
  }

  return (
    <Layout user={user}>
      <div className="max-w-2xl mx-auto px-6 py-8">
        <BackButton fallback="/" />
        <div className="mb-8">
          <h1 className="text-2xl font-bold tracking-tight">Profile</h1>
          <p className="text-sm text-gray-400 mt-1">
            Manage your personal details. Changes are saved to your account.
          </p>
        </div>

        {error && (
          <div className="mb-6 p-3 rounded-lg bg-orange-950/40 border border-orange-500/40 text-orange-400 text-xs">
            {error}
          </div>
        )}

        <form onSubmit={handleSave} className="bg-white/[0.02] border border-white/10 rounded-xl p-6 space-y-6">
          <div>
            <label className="block text-xs text-gray-400 uppercase tracking-wider mb-2">Email</label>
            <input
              type="email"
              value={profile.email}
              readOnly
              className="w-full bg-white/[0.03] border border-white/10 rounded-lg px-4 py-3 text-sm text-gray-300 focus:border-[#645DF6] transition-colors disabled:bg-transparent"
            />
          </div>

          <div>
            <label className="block text-xs text-gray-400 uppercase tracking-wider mb-2">Full Name</label>
            <input
              type="text"
              value={profile.name || ''}
              onChange={e => setProfile({ ...profile, name: e.target.value } as UserProfile)}
              placeholder="Your full name"
              className="w-full bg-white/[0.03] border border-white/10 rounded-lg px-4 py-3 text-sm text-white placeholder-gray-500 focus:border-[#645DF6] transition-colors"
            />
          </div>

          <div>
            <label className="block text-xs text-gray-400 uppercase tracking-wider mb-2">Phone</label>
            <input
              type="tel"
              value={profile.phone || ''}
              onChange={e => setProfile({ ...profile, phone: e.target.value } as UserProfile)}
              placeholder="+1 (555) 000-0000"
              className="w-full bg-white/[0.03] border border-white/10 rounded-lg px-4 py-3 text-sm text-white placeholder-gray-500 focus:border-[#645DF6] transition-colors"
            />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
            <div>
              <label className="block text-xs text-gray-400 uppercase tracking-wider mb-2">Company</label>
              <input
                type="text"
                value={profile.company || ''}
                onChange={e => setProfile({ ...profile, company: e.target.value } as UserProfile)}
                placeholder="Company or organization"
                className="w-full bg-white/[0.03] border border-white/10 rounded-lg px-4 py-3 text-sm text-white placeholder-gray-500 focus:border-[#645DF6] transition-colors"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-400 uppercase tracking-wider mb-2">Department</label>
              <input
                type="text"
                value={profile.department || ''}
                onChange={e => setProfile({ ...profile, department: e.target.value } as UserProfile)}
                placeholder="Department or team"
                className="w-full bg-white/[0.03] border border-white/10 rounded-lg px-4 py-3 text-sm text-white placeholder-gray-500 focus:border-[#645DF6] transition-colors"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs text-gray-400 uppercase tracking-wider mb-2">Time Zone</label>
            <select
              value={profile.time_zone || 'UTC'}
              onChange={e => setProfile({ ...profile, time_zone: e.target.value } as UserProfile)}
              className="w-full bg-white/[0.03] border border-white/10 rounded-lg px-4 py-3 text-sm text-white focus:border-[#645DF6] transition-colors"
            >
              <option value="UTC">UTC</option>
              <option value="America/New_York">Eastern (New York)</option>
              <option value="America/Chicago">Central (Chicago)</option>
              <option value="America/Denver">Mountain (Denver)</option>
              <option value="America/Los_Angeles">Pacific (Los Angeles)</option>
              <option value="Europe/London">London</option>
              <option value="Europe/Berlin">Berlin</option>
              <option value="Asia/Kolkata">India (Kolkata)</option>
              <option value="Asia/Singapore">Singapore</option>
              <option value="Asia/Tokyo">Tokyo</option>
              <option value="Australia/Sydney">Sydney</option>
            </select>
          </div>

          <button
            type="submit"
            disabled={saving}
            className="w-full btn-shimmer py-3 rounded-lg text-sm font-medium text-white cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {saving ? 'Saving…' : 'Save Changes'}
          </button>

          <p className="text-xs text-gray-500 text-center">
            {profile.updated_at ? `Last updated: ${new Date(profile.updated_at).toLocaleString()}` : 'Never saved'}
          </p>
        </form>
      </div>
    </Layout>
  )
}
