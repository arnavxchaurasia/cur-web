# CUR Web App — Design Spec
_2026-05-07_

## Overview

A web app where Google FSRs/CEs upload an AWS CUR or Cost Explorer export, the `aws-gcp-cost-projection` skill runs headlessly, and the resulting `report.html` is available for download. Facets captures telemetry (prospect name + AWS spend) per upload via a Slack webhook to track the GTM pipeline.

---

## Architecture

```
Browser (FSR/CE @ google.com)
    ↕
Cloudflare  (DNS + HTTPS termination)
    ↕
GCE VM :80
  nginx
    ├── /*       → /var/www/cur-web/          (React SPA build)
    └── /api/*   → localhost:8080             (Go binary)

  Go binary :8080
    ├── Auth middleware (session cookie)
    ├── HTTP handlers (6 routes)
    └── Job goroutines (one per upload)
          → exec claude in /var/data/cur-web/jobs/{id}/
```

**Stack:**
- **Backend:** Go, Chi router, single binary
- **Frontend:** Vite + React, served as static files by nginx from `/var/www/cur-web/`
- **Database:** SQLite at `/var/data/cur-web/cur-web.db`
- **Job storage:** Local filesystem at `/var/data/cur-web/jobs/{id}/`
- **Auth:** Google OAuth 2.0, `hd=google.com` enforced
- **Email:** SendGrid — job-ready and job-failed notifications to rep
- **Telemetry:** Slack webhook — success (prospect + spend) and failure (prospect + rep + job ID) to Facets internal channel
- **TLS:** Cloudflare proxy; nginx serves plain HTTP on :80

---

## Data Model

### SQLite — `jobs` table

```sql
CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,           -- uuid v4
    owner       TEXT NOT NULL,              -- rep's google email
    prospect    TEXT NOT NULL,              -- entered at upload time
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
    input_ext   TEXT,                       -- original file extension (.csv, .zip)
    aws_spend   REAL,                       -- extracted from report on done
    error       TEXT,                       -- last 500 bytes of claude.log on failure
    created_at  DATETIME DEFAULT (datetime('now')),
    updated_at  DATETIME DEFAULT (datetime('now'))
);
```

### Filesystem — `/var/data/cur-web/`

```
cur-web.db
jobs/
  {job_id}/
    input.{ext}          ← uploaded CUR file
    report.html          ← produced by claude (exists when status=done)
    claude.log           ← stdout+stderr of claude process (always kept)
    projection-audit/    ← claude's working files (duckdb, mappings, etc.)
```

Job ID is the SQLite primary key. File paths are derived deterministically from the ID — no path stored in DB.

---

## API Routes

All routes except `/api/auth/login` and `/api/auth/callback` require a valid session cookie.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/auth/login` | Redirect to Google OAuth consent (`hd=google.com`, PKCE state) |
| GET | `/api/auth/callback` | Exchange code → set encrypted session cookie → redirect `/` |
| GET | `/api/auth/logout` | Clear cookie → redirect `/` |
| GET | `/api/auth/me` | `{email, name}` — used by React to bootstrap session state |
| POST | `/api/jobs` | Multipart: `file` + `prospect_name` → create job, spawn goroutine, return `{id, status}` |
| GET | `/api/jobs` | List caller's jobs, newest first: `[{id, prospect, status, created_at, aws_spend}]` |
| GET | `/api/jobs/:id` | Single job: `{id, prospect, status, created_at, updated_at, error}` |
| GET | `/api/jobs/:id/download` | Verify `owner == session.email` → stream `jobs/{id}/report.html` as attachment |

---

## Job Lifecycle

```
POST /api/jobs received
  → write input file to /var/data/cur-web/jobs/{id}/input.{ext}
  → INSERT INTO jobs (status='pending')
  → spawn goroutine
  → return {id, status: 'pending'}

Goroutine:
  1. UPDATE jobs SET status='running'
  2. exec.Command("claude", "--dangerously-skip-permissions", "-p",
       "Use the aws-gcp-cost-projection skill to project costs from ./input.{ext}")
     Dir = /var/data/cur-web/jobs/{id}/
     Env = ANTHROPIC_API_KEY=...
     Stdout+Stderr → jobs/{id}/claude.log

  3a. Exit 0 AND report.html exists:
      → parse aws_spend from report.html
      → UPDATE jobs SET status='done', aws_spend=X
      → SendGrid: email rep download link
      → Slack: "✅ {prospect} — ${spend}/mo — {rep}"

  3b. Exit != 0 OR report.html missing:
      → UPDATE jobs SET status='failed', error=<last 500 bytes of claude.log>
      → SendGrid: email rep "report failed, we're looking into it"
      → Slack: "❌ Job failed — prospect: {prospect}, rep: {email}, job: {id}"
```

Multiple jobs run concurrently on the same VM, each in an isolated directory. No queue.

---

## Auth Flow

```
1. GET /api/auth/login
   → redirect to Google OAuth with hd=google.com + PKCE state cookie

2. GET /api/auth/callback
   → verify state (CSRF)
   → exchange code for tokens
   → GET userinfo (email, name, hd)
   → assert hd == "google.com"  ← enforced server-side, never trust client
   → set encrypted session cookie (email + name, 8h TTL)
   → redirect /

3. Middleware on every API route:
   → decrypt + validate cookie
   → expired/missing/invalid → 401
   → attach {email, name} to request context
```

Session is stateless — nothing stored server-side. Cookie encrypted with AES-GCM using a key from config.

---

## Startup Verification

On `main()`, before binding :8080, the binary verifies:

1. `claude` is on PATH (`exec.LookPath`)
2. Skill `SKILL.md` exists at `cfg.SkillDir` (configurable, default `~/.claude/skills/aws-gcp-cost-projection`)
3. `preflight.sh --check-only` exits 0 (verifies `duckdb`, `jq`, `gzip` on PATH)

If any check fails, the binary exits with a descriptive error. This prevents accepting uploads that would silently fail.

---

## Configuration

All config via environment variables (`.env` file in dev, systemd `EnvironmentFile` in prod):

```
PORT=8080
DATA_DIR=/var/data/cur-web
SKILL_DIR=/home/ubuntu/.claude/skills/aws-gcp-cost-projection
ANTHROPIC_API_KEY=...
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=https://cur.facets.cloud/api/auth/callback
SESSION_SECRET=...          # 32-byte random, AES-GCM key
SENDGRID_API_KEY=...
SENDGRID_FROM=noreply@facets.cloud
APP_BASE_URL=https://cur.facets.cloud
SLACK_WEBHOOK_URL=...
```

---

## Frontend — UI Screens

Four screens, React SPA, Facets branded (#645DF6 purple, #00C2BB teal). Logo present in nav on all authenticated pages.

1. **Login** — full-height dark background, Facets logo centered, Google sign-in button, "@google.com accounts only" label
2. **Upload** — nav with logo + signed-in email, prospect name field, drag-drop file zone, "Generate Report" CTA (gradient button), past reports list below
3. **Running** — prospect name in heading, teal "Running" indicator, "10–30 min, we'll email you, you can close this tab" message, job ID shown
4. **Done** — prospect name, AWS spend summary card, download button (gradient), past reports list

Status polling: React `useEffect` calls `GET /api/jobs/:id` every 5 seconds while `status === 'running'`.

### Implementation Notes
- Login page: dark background must be full viewport height (not content-height)
- All authenticated pages: Facets logo (F icon or wordmark) in top-left nav

---

## Deployment

**Frontend:**
```bash
cd frontend && npm run build
rsync -av dist/ user@vm:/var/www/cur-web/
```

**Backend:**
```bash
GOOS=linux GOARCH=amd64 go build -o cur-web ./cmd/server
scp cur-web user@vm:/usr/local/bin/cur-web
ssh user@vm "sudo systemctl restart cur-web"
```

**nginx config:** proxy `/api/*` to `localhost:8080`, serve `/var/www/cur-web/` for all other paths (SPA fallback: `try_files $uri /index.html`).

**systemd unit:** runs Go binary as a non-root user, `EnvironmentFile=/etc/cur-web/env`, `Restart=on-failure`.

---

## Out of Scope (v1)

- Multi-tenant admin dashboard
- Re-run / version history per prospect
- Custom GCP region or discount overrides in UI
- Full report attached to email (link only)
- Report TTL / cleanup
- CI/CD pipeline
