# TEST PROMPT — Admin panel, end to end

**You are a QA engineer testing the admin panel of a web app you have never
seen. Read sections 0–2 before testing anything.**

Your job is to find defects, not to confirm things work. A test that passes
tells us little; a test that fails tells us everything. **Report what you
observe, never what you assume.** If you cannot run a test, say so — do not
mark it passed.

---

## 0. What this app is

A tool that takes an AWS bill and estimates what the same workload would cost
on Google Cloud. A **job** is one bill being processed through a pipeline that
ends in an HTML report.

| part | where |
|---|---|
| Go API server | `internal/`, `cmd/server/` |
| React UI | `frontend/src/` |
| Python pipeline | `skill/aws-gcp-cost-projection/scripts/` |

### Roles — you must understand these before testing

| role | how it is decided |
|---|---|
| **anonymous** | no session |
| **user** | signed in, owns their own jobs |
| **admin** | `Session.IsAdmin()` is true |
| **facets employee** | email ends `@facets.cloud` or `@facets.io` |

`IsAdmin()` is true when **any** of these holds:
1. the email is in the `ADMIN_EMAILS` env list, **or**
2. role is `admin` **and** the user is a facets employee, **or**
3. role is `admin` **and** `admin_granted_by` is set (an external admin that a
   facets admin explicitly created)

**You need at least four test accounts** to cover the matrix:
- A: facets admin (`a@facets.cloud`, role admin)
- B: facets non-admin (`b@facets.cloud`, role viewer)
- C: external non-admin (`c@gmail.com`, role viewer)
- D: external user who owns a job (used as the "other owner")

---

## 1. Running it

```bash
# backend
cd <repo>
DEV_AUTH_BYPASS=true ADMIN_EMAILS=a@facets.cloud go run ./cmd/server

# frontend
cd frontend && npm run dev
```

`DEV_AUTH_BYPASS=true` relaxes the OAuth env requirements for local testing.

**Before you start, get a baseline:**

```bash
go build ./... && go test ./internal/...
cd skill/aws-gcp-cost-projection && python -m pytest tests/ -q
cd frontend && npm run build        # runs tsc -b
```

Record the numbers. If anything is already failing, **report that first** —
do not start feature testing on a broken baseline.

**You need at least 2–3 completed jobs** to test most of this. Create them by
uploading bills through the UI. If you have no sample bills, say so and test
what you can without them — do not fabricate data.

---

## 2. The rules this app is supposed to follow

These are design decisions, not accidents. **A violation of any one is a
defect, even if the feature appears to work.**

### Rule 1 — "Not yours" must look exactly like "does not exist"
Requesting a job you do not own returns **404**, with a body byte-identical to
requesting a job id that was never created. A **403** here is a bug: it
confirms the id is real.

### Rule 2 — Hiding something never changes a number
Toggling a report column or section off changes what is *displayed*. Every
total stays identical.

### Rule 3 — Settings apply to future renders, never retroactively
Changing a setting does not alter an already-generated report. A previously
shared link must keep showing what the recipient originally saw.

### Rule 4 — An honest refusal beats a fake success
Where an action cannot work, the UI must say so. A success toast for something
that did not happen is a defect.

### Rule 5 — Nobody can escalate themselves
No action may grant a permission to the caller.

---

## 3. THE AUTHORIZATION MATRIX — test this first

This is the highest-value section. Do it before any feature testing.

For **every** route below, test as: anonymous, user C (external non-admin),
user B (facets non-admin), and user A (admin).

```
GET    /api/admin/analytics
GET    /api/admin/assumptions
PUT    /api/admin/assumptions/{key}
GET    /api/admin/assumptions/history
GET    /api/admin/audit
GET    /api/admin/bills
GET    /api/admin/bills/{hash}/download | /preview | /view
GET    /api/admin/catalog/status
POST   /api/admin/catalog/refresh
GET    /api/admin/deleted-jobs
GET    /api/admin/jobs
POST   /api/admin/jobs/{id}/cancel
DELETE /api/admin/jobs/{id}
POST   /api/admin/jobs/cancel-all | resume-all
GET    /api/admin/jobs/{id}/compare
GET    /api/admin/jobs/{id}/diagnostics | /diagnostics/records
POST   /api/admin/jobs/{id}/regenerate-report
GET    /api/admin/notifications | /notify/status
POST   /api/admin/notify/test
GET    /api/admin/report-settings | /history
PUT    /api/admin/report-settings/{key}
POST   /api/admin/report-settings/reset
GET    /api/admin/shares
DELETE /api/admin/shares/{token}
GET    /api/admin/system/state
GET    /api/admin/tickets | /tickets/{id}
POST   /api/admin/tickets
PUT    /api/admin/tickets/{id}
GET    /api/admin/users
POST   /api/admin/users/{email}/role
```

**Expected:** anonymous → 401. C and B → **403** (they are not admins; these
are admin-namespace routes, so the id-disclosure concern of Rule 1 does not
apply). A → 200.

**Record every deviation in a table.** A single route that returns 200 for B
or C is a serious finding — report it immediately rather than continuing.

### Also test the UI routes
Visit each as C (non-admin): `/admin`, `/admin/analytics`, `/admin/assumptions`,
`/admin/audit`, `/admin/bills`, `/admin/catalog`, `/admin/notifications`,
`/admin/report-settings`, `/admin/tickets`, `/admin/users`.

**Expected:** redirected away, not rendered. A page that renders and then shows
API errors is a defect — report the route name.

---

## 4. Feature tests

For each: do the steps, write down what you saw, and compare to "Expected".

### 4.1 Users and roles

1. As A, open `/admin/users`. The list loads with emails and roles.
2. Change C's role from `viewer` to `admin`.
   **Expected:** **refused**, with a message saying admin is restricted to
   @facets accounts *or* that only a facets admin may grant it. Read the exact
   message and record it.
3. Repeat as A but verify whether an **external grant** path exists (the
   feature is "a facets admin may grant admin to a non-facets user"). If the
   grant succeeds, then:
   - **Expected:** C is admin on the **next request, without logging out**.
     Verify by calling an admin route as C immediately.
   - The users list must show C distinctly — e.g. "granted by a@facets.cloud"
     — not a bare "admin".
4. As newly-admin C, try to grant admin to another external user.
   **Expected: refused.** Only facets admins may create external admins
   (Rule 5 — otherwise external accounts promote each other).
5. As A, try to change **your own** role.
   **Expected: refused.**
6. Demote B (facets) from admin to viewer while B is signed in.
   **Expected:** B loses admin access on the **next request**, with no
   re-login.
7. Try to demote the **last remaining facets admin**.
   **Expected: refused** with a clear message. If this succeeds, it is a
   lockout bug — **stop and report immediately.**
8. Every grant/demote appears in `/admin/audit` with who did it.

> **Known quirk to verify, not assume:** a user listed in the `ADMIN_EMAILS`
> env var is admin regardless of their stored role. Demote such a user to
> `viewer` and check what happens. Report the behaviour you observe and
> whether the UI gives any hint — do not judge it right or wrong.

### 4.2 Assumptions

1. `/admin/assumptions` lists assumptions with title, value, unit, rationale.
2. Change `aurora_acu_vcpu` to a valid value. **Expected:** saves; appears in
   history with your email.
3. Enter a value **below the minimum** and above the maximum.
   **Expected:** refused both times, value unchanged on reload.
4. Enter a non-numeric value. **Expected:** refused.
5. **Reload the page.** The change persisted.
6. **Restart the server.** The change still persisted (it must not be reset by
   re-seeding).
7. Run a **new** job after the change. Its report should reflect the new
   assumption. An **older** job's report must be unchanged (Rule 3).

### 4.3 Report settings

1. `/admin/report-settings` shows tick boxes grouped into sections, columns
   and rows.
2. Note the **GCP 3-Year CUD** total on an existing report. Turn the
   `col_gcp_cud_3yr` column **off**.
3. Regenerate that job's report (see 4.4).
   **Expected:** the column is gone from the table **and the 3-Year CUD
   summary total is byte-identical** (Rule 2). If the total moved, that is a
   serious defect.
4. Turn a section off (e.g. discounts). Regenerate.
   **Expected:** the section is gone; **every remaining number is identical**.
5. Set a row filter (`row_min_cost_usd` = 100). Regenerate.
   **Expected:** small rows disappear from the table, totals do **not** change,
   and the report prints a line saying how many rows were hidden and their
   value. **A filtered table with no such disclosure is a defect.**
6. Turn **every** column off. **Expected:** the report still renders with at
   minimum the service and cost columns — never an empty table.
7. Press **Reset to defaults**. Everything returns to its default.

### 4.4 Regenerate report

1. On a completed job as A, press **Regenerate report**.
   **Expected:** a new report version appears; it takes seconds, not minutes.
2. **The previous report is still downloadable** by its run id (Rule 3).
3. Compare old and new: only the settings you changed differ. **All totals
   identical** unless you changed something that legitimately affects them.
4. Regenerate a job whose files have been deleted.
   **Expected:** a clear error, not a 500 or a hang.
5. Check `/admin/audit` records the regeneration.

### 4.5 Job health / diagnostics

1. Open a completed job as A. Find the **Job Health** panel.
2. **Expected:** a verdict line (errors / warnings / none), and a table of
   findings by code with counts, row counts and dollar exposure.
3. Expand a finding. **Expected:** individual messages, including detail such
   as a file line number or resource id.
4. Find a job with **no** findings.
   **Expected:** "No issues recorded" — a clear green/none state.
5. Delete a job's directory on disk, then reopen the panel.
   **Expected:** it says diagnostics are **unavailable / expired** — clearly
   different from "no issues". **If a job with missing files renders as
   "clean", that is a defect** — it reports a problem as an all-clear.

### 4.6 Share links

1. On a job, create a share link. Open it in a **private window** (no session).
   **Expected:** the report loads.
2. `/admin/shares` lists it. **Expected: the token is masked** (only a short
   prefix shown). A full working token in the admin list is a defect.
3. **Revoke** it. Reload the shared link.
   **Expected:** it no longer works.
4. Compare the revoked-link response to a **made-up** token's response.
   **Expected: identical.** A different message for a revoked vs
   never-existed token leaks whether the token was real.
5. Check the revocation appears in the audit log.
6. If a link has an expiry, find or set one in the past and confirm it stops
   working with the same "not found" response.

### 4.7 Catalog

1. `/admin/catalog` shows the GCP catalog status and age.
2. Press **Refresh**.
   **Expected:** either it starts, **or** it refuses with a readable sentence
   explaining why (on some deployments a runtime refresh cannot persist).
   **Raw JSON shown to the user is a defect. A success message for a refresh
   that did not happen is a worse defect** (Rule 4).
3. Check `/admin/audit` records the attempt either way.

### 4.8 Notifications

1. `/admin/notifications` shows per-channel configured / not configured.
   **Expected: no secret values displayed** — only whether a key is present.
2. Press **Send test notification**.
   **Expected:** success, or the real error. Not a silent no-op.
3. If email/Slack are configured, complete a job and confirm a notification
   arrives.
4. Unset a channel's env var, restart, reload the page.
   **Expected:** it now reports that channel as not configured. A page that
   still claims it is working is a defect.

### 4.9 Jobs administration

1. `/admin/jobs` lists jobs across **all** users (admin view) with status and
   owner.
2. Cancel a running job. **Expected:** it stops; status updates.
3. **Cancel all** / **Resume all** — verify they do what they say.
4. Delete a job. **Expected:** it disappears and shows in `/admin/deleted-jobs`.
5. **Compare two runs** of one job. **Expected:** per-service differences plus
   a header showing what changed in the inputs (assumptions version, settings
   version, catalog vintage). A diff with no input context is much less useful
   — note it if that header is missing.

### 4.10 Bills library

1. `/admin/bills` lists uploaded bills.
2. Preview, view and download each.
   **Expected:** the right file, right content type, no path traversal.
3. Try a made-up hash. **Expected:** clean 404, no stack trace.

### 4.11 Tickets

1. `/admin/tickets` as A: list loads. Create, open and update a ticket.
2. **As C (non-admin), visit `/admin/tickets` directly.**
   **Expected:** redirected away. *(This was recently fixed; confirm it stayed
   fixed.)*
3. Call `GET /api/admin/tickets` as C. **Expected: 403.**

### 4.12 Audit log and analytics

1. `/admin/audit` lists actions with who, what, when. Paging works.
2. Perform three admin actions, reload. **Expected:** all three appear, newest
   first, attributed to the right user.
3. `/admin/analytics` loads with real numbers. Cross-check one against another
   source (e.g. job count vs `/admin/jobs`).

### 4.13 Quotas

1. Find `max_jobs_per_day` and `max_concurrent_jobs` in settings.
2. Set `max_jobs_per_day` to 1. As a **non-admin**, upload twice.
   **Expected:** the second is refused with **429**, a clear message naming the
   limit and when it resets.
3. As an **admin**, confirm the limit does not apply.

---

## 5. Negative and robustness tests

1. **Path traversal** — `/api/admin/bills/..%2f..%2fetc%2fpasswd/view` and
   similar on every `{id}` / `{hash}` / `{token}` route. **Expected:** 404,
   never file contents, never a stack trace.
2. **Nonexistent ids** on every admin route. **Expected:** clean 404s.
3. **Malformed JSON** bodies on every PUT/POST. **Expected:** 400, not 500.
4. **Wrong types** — a string where a number is expected. **Expected:** 400.
5. **Double submit** — press Regenerate twice quickly. **Expected:** no
   duplicate run or corruption.
6. **Session expiry** — delete the session cookie mid-session and act.
   **Expected:** 401 and a redirect to login, not a blank page.
7. **Responsive** — check each admin page at phone width. Nothing clipped or
   unreachable.
8. **Browser console** — keep it open the whole time. Report every error.

---

## 6. How to report

Produce a table:

| # | Area | What I did | Expected | Observed | Verdict |
|---|---|---|---|---|---|

Then:

- **Defects**, each with: exact steps, the request/response if it is an API
  issue, severity (does it leak data, lose data, mislead a user, or just look
  wrong), and whether it is reproducible.
- **Could not test**, with the reason. This section is as important as the
  defects — an untested area that looks tested is how bugs ship.
- **Baseline numbers** from §1, before and after.

### Severity guidance
- **Critical** — one user sees another's data; a non-admin reaches an admin
  action; an admin lockout is possible
- **High** — a number is wrong; a success message for something that did not
  happen; a missing-data state rendered as "all clear"
- **Medium** — a feature does not work but fails visibly
- **Low** — cosmetic

**Do not fix anything.** Report only. A fix made mid-test invalidates every
result that followed it.
