# Cost Engine API

Personal access tokens authenticate REST API calls to the Cost Engine without a
session cookie. Use them to connect scripts, CI, or other tools.

## Base URL

The API is served at the same origin as the web app (e.g.
`https://api.cost-engine.example.com` or `http://localhost:8080` locally). All
paths below are relative to that origin.

## Authentication

`Authorization: Bearer fct_live_<key_id>_<secret>`

Tokens are created in the UI at `/profile/tokens` (or via `POST /api/user/tokens`
with an interactive session). The full token is shown exactly once, at creation
time, and is never stored — only its SHA-256 hash is. If you lose it, revoke it
and create a new one.

## Scopes

Scopes narrow what a token can do **within** what your account can already do;
they never widen it. Token-authenticated requests are additionally subject to
`RequireScope` on the routes below. Cookie-authenticated (interactive session)
requests pass scope checks unconditionally.

| Scope | Allows |
|---|---|
| `jobs:read` | List your jobs, read a job, fetch job logs |
| `jobs:write` | Create a job |
| `reports:read` | (reserved) download report artifacts |

## Routes

### Tokens

`GET /api/user/tokens`
- Scope: none (session only — tokens cannot list tokens)
- Returns: `{"tokens":[APIToken, ...]}` — metadata only, never the secret.
- Cache: `Cache-Control: no-store`

`POST /api/user/tokens`
- Scope: none (session only)
- Body: `{"name": string, "scopes": string[], "ttl_days": 30|90|365|0}`
- Returns `201`: `{"token": "fct_live_...", "meta": APIToken}` — **the only time the full secret is returned**.
- Rate limit: 10 per hour per user.

`DELETE /api/user/tokens/{key_id}`
- Scope: none (session only)
- Returns: `204 No Content`.
- Revoking is a soft delete; the row stays for the audit trail.

### Jobs (token-reachable)

`GET /api/jobs`
- Scope: `jobs:read`
- List your own jobs (owner-filtered).

`GET /api/jobs/{id}`
- Scope: `jobs:read`
- Get a single job (`canAccess` enforces ownership).

`GET /api/jobs/{id}/logs`
- Scope: `jobs:read`
- Fetch job logs.

`POST /api/jobs`
- Scope: `jobs:write`
- Create and upload a bill to start a job.

## Errors

| Status | Meaning | Body |
|---|---|---|
| `401` | Unauthorized — missing/invalid/revoked/expired token or no session | `{"error":"..."}` |
| `403` | Forbidden — token on an admin route, or insufficient scope | `{"error":"..."}` |
| `429` | Rate limit exceeded | `{"error":"rate limit exceeded"}` + `Retry-After` header |

A revoked token yields `401 {"error":"token revoked"}`; an expired token yields
`401 {"error":"token expired"}`.

## Rate limits

| Endpoint | Limit | Keyed by |
|---|---|---|
| `POST /api/user/tokens` | 10/hour | user |
| Token-authenticated API calls | 600/minute | key id (not user) |

Token-authenticated calls are limited per key id so one runaway script cannot
lock you out of your other integrations.

## Admin routes

Admin routes (`/api/admin/*`) reject bearer tokens entirely with
`403 {"error":"admin routes require an interactive session"}`. They require an
interactive session cookie.

## MCP

MCP (Model Context Protocol) access is intentionally **out of scope** for this
release. A token authenticates REST API calls today; an MCP server is a thin
layer over these same endpoints and is tracked separately.

## Example

```bash
# Create a token in the UI at /profile/tokens, then:
curl -H "Authorization: Bearer fct_live_a1b2c3d4e5f6_..." \
  https://api.example.com/api/jobs
```
