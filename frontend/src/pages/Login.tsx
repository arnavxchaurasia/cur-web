import facetsLogo from '../assets/facets-logo-full.svg'
import facetsFIcon from '../assets/facets-logo-f.svg'
import { useTitle } from '../lib/useTitle'

const FEATURES = [
  'Per-line-item AWS → GCP service mapping',
  'On-demand, 1-year, and 3-year CUD pricing',
  'Customer-shareable HTML report in minutes',
]

export function Login() {
  useTitle('Sign in')
  return (
    <div className="min-h-screen flex">
      {/* left panel — light, like a conventional login form */}
      <div className="w-full lg:w-[440px] shrink-0 bg-white text-gray-900 flex flex-col px-10 sm:px-14 py-10 relative z-10">
        <img src={facetsLogo} alt="Facets" className="h-6 anim-fade-in-up" />

        <div className="flex-1 flex flex-col justify-center">
          <div className="max-w-sm w-full flex flex-col gap-8">
            <div className="anim-fade-in-up delay-100">
              <h1 className="text-3xl font-semibold tracking-tight text-gray-900">Sign in</h1>
              <p className="text-[#645DF6] text-xs tracking-widest uppercase font-medium mt-3">
                AWS → GCP Cost Estimator
              </p>
            </div>

            <button
              onClick={() => { window.location.assign('/api/auth/login') }}
              className="cursor-pointer flex items-center justify-center gap-3 bg-white text-gray-700 px-6 py-3.5 rounded-lg font-medium
                border border-gray-300 hover:bg-gray-50 hover:border-gray-400 hover:scale-[1.02] active:scale-[0.99]
                transition-all duration-150 anim-fade-in-up delay-250"
            >
              <GoogleIcon />
              Sign in with Google
            </button>

            <p className="text-xs text-gray-400 anim-fade-in-up delay-325">@google.com or @facets.cloud accounts only</p>
          </div>
        </div>

        <p className="text-xs text-gray-400 anim-fade-in delay-500">© {new Date().getFullYear()} Facets.cloud</p>
      </div>

      {/* right panel — dark, animated, no static illustration */}
      <div className="login-bg hidden lg:flex flex-1 flex-col items-center justify-center relative overflow-hidden px-16 text-white">
        <div className="login-orb absolute top-1/4 left-1/4 w-96 h-96 rounded-full pointer-events-none"
             style={{ background: 'radial-gradient(circle, rgba(100,93,246,0.16) 0%, transparent 70%)' }} />
        <div className="login-orb absolute bottom-1/4 right-1/4 w-80 h-80 rounded-full pointer-events-none"
             style={{ background: 'radial-gradient(circle, rgba(0,194,187,0.12) 0%, transparent 70%)', animationDelay: '5s' }} />

        <div className="relative flex flex-col items-center mb-10">
          <div className="login-drop-line" />
          <div className="login-drop-dot" />
          <div className="login-glyph-wrap relative rounded-full">
            <img src={facetsFIcon} alt="" className="h-20 relative z-10" />
          </div>
        </div>

        <div className="max-w-md relative z-10 text-center anim-fade-in-up delay-175">
          <p className="login-eyebrow text-xs uppercase tracking-[0.3em] text-[#00C2BB] mb-4">AI-Powered Migration</p>
          <h2 className="text-3xl font-semibold leading-tight tracking-tight">
            Estimate cloud costs <span className="login-gradient-text">10x faster</span>
          </h2>
          <p className="text-gray-400 text-sm mt-4 leading-relaxed">
            An AI agent classifies every line item on your AWS bill, maps it to its
            Google Cloud equivalent, and prices it out — on-demand and committed-use.
          </p>

          <ul className="mt-8 flex flex-col gap-3 items-start mx-auto w-fit text-left">
            {FEATURES.map((f, i) => (
              <li key={f} className="login-feature flex items-center gap-3 text-sm text-gray-300"
                  style={{ animationDelay: `${350 + i * 120}ms` }}>
                <span className="login-feature-dot shrink-0 w-1.5 h-1.5 rounded-full bg-[#645DF6]"
                      style={{ animationDelay: `${1000 + i * 220}ms` }} />
                {f}
              </li>
            ))}
          </ul>
        </div>

        {/* "Powered by Facets" — doubles as a discreet secondary entry point
            into the same Google sign-in flow. There's no separate admin
            login: server-side, the resulting session is granted admin only
            if its email is in ADMIN_EMAILS (see auth.Session.IsAdmin) — this
            button is just an alternate, low-visibility way in for that case. */}
        <button
          onClick={() => { window.location.assign('/api/auth/login?next=/facets') }}
          className="cursor-pointer group absolute bottom-6 right-8 flex items-center gap-2 text-xs text-gray-500
            hover:text-gray-300 transition-colors duration-200 z-10"
          title="Sign in"
        >
          Powered by
          <img src={facetsFIcon} alt="Facets"
               className="h-4 opacity-80 transition-transform duration-200 group-hover:scale-110" />
        </button>
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
