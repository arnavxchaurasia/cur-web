import facetsLogo from '../assets/facets-logo-full.svg'
import { useTitle } from '../lib/useTitle'

export function Login() {
  useTitle('Sign in')
  return (
    <div className="login-bg min-h-screen text-white flex flex-col items-center justify-center px-6 relative overflow-hidden">
      {/* ambient orbs */}
      <div className="login-orb absolute top-1/4 left-1/4 w-96 h-96 rounded-full pointer-events-none"
           style={{ background: 'radial-gradient(circle, rgba(100,93,246,0.08) 0%, transparent 70%)' }} />
      <div className="login-orb absolute bottom-1/4 right-1/4 w-80 h-80 rounded-full pointer-events-none"
           style={{ background: 'radial-gradient(circle, rgba(0,194,187,0.07) 0%, transparent 70%)', animationDelay: '5s' }} />

      <div className="max-w-md w-full flex flex-col items-center gap-6 text-center relative z-10">
        <img src={facetsLogo} alt="Facets" className="h-8 anim-fade-in-up" />
        <p className="text-[#645DF6] text-sm tracking-widest uppercase font-medium anim-fade-in-up delay-100">
          AI-powered AWS → GCP Cost Estimator
        </p>
        <p className="text-sm text-gray-400 leading-relaxed anim-fade-in-up delay-175">
          Upload an AWS Cost &amp; Usage Report or Cost Explorer export. An AI agent classifies
          each line item, maps it to its GCP equivalent, applies on-demand and 1/3-year committed-use
          discounts, and produces a customer-shareable HTML report.
        </p>
        <button
          onClick={() => { window.location.assign('/api/auth/login') }}
          className="flex items-center gap-3 bg-white text-gray-800 px-6 py-3 rounded-lg font-medium
            hover:bg-gray-50 hover:scale-[1.02] active:scale-[0.99] shadow-lg shadow-black/30
            transition-all duration-150 mt-2 anim-fade-in-up delay-250"
        >
          <GoogleIcon />
          Sign in with Google
        </button>
        <p className="text-xs text-gray-600 anim-fade-in-up delay-325">@google.com or @facets.cloud accounts only</p>
        <p className="text-xs text-gray-600 leading-relaxed mt-4 anim-fade-in-up delay-400">
          Outputs are AI-generated estimates and may vary — treat the report as
          directional and verify before sharing.
        </p>
      </div>
    </div>
  )
}

function GoogleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48">
      <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.07 17.74 9.5 24 9.5z"/>
      <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>
      <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/>
      <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.18 1.48-4.97 2.31-8.16 2.31-6.26 0-11.57-3.59-13.46-8.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>
    </svg>
  )
}
