# WORK ORDER — Post-Review Fixes (v2, re-issued 2026-10-30)

**Who this is for:** an implementation model with no prior context on this repo.
**Why v2:** the v1 of this work order had 5 items. Only Item 3 was implemented
(`internal/jobs/watcher.go`, run-id fix — verified correct, leave it alone).
Items 1, 2, 4, 5 were **not** done and are re-issued here with exact code.

**Repo root:** `C:\Users\arnav\Downloads\cur-web-AGY`
**Branch:** `fix/spec-dispatch-cutover-review`
**Language:** Go (server) — none of these items touch Python.

Do the items **in the order given**. Item 1 is a security fix; do it first.
After each item run `go build ./... && go test ./...` before starting the next.

---

## Item 0 (do this before anything else) — commit the untracked test file

There is an untracked file `internal/jobs/admin_panel_testplan_test.go`.
It contains `TestSection3_AuthorizationMatrix` and sibling tests. An untracked
test file never runs in CI, so the authorization coverage it provides is
currently worth nothing.

**Action:** `git add internal/jobs/admin_panel_testplan_test.go` and include it
in the first commit you make for this work order. Do not edit its contents.

**Check:** `git status --porcelain internal/jobs/` prints nothing untracked.

---

## Item 1 — `strings.Contains(email, "@facets.")` is an authentication bypass (SECURITY, DO FIRST)

### The bug

Seven places in the codebase decide "is this person a Facets employee?" with:

```go
strings.HasSuffix(lower, "@facets.cloud") || strings.HasSuffix(lower, "@facets.io") || strings.Contains(lower, "@facets.")
```

The first two clauses are correct. The third clause, `strings.Contains`, matches
**anywhere in the string**. So all of these pass as Facets employees:

| attacker-controlled address | why it passes |
|---|---|
| `evil@facets.attacker.com` | contains the substring `@facets.` |
| `x@facets.evil.ru` | same |
| `a@facets.co.uk.phish.io` | same |

A Facets employee is auto-granted `role = "admin"` on first login
(`internal/auth/oauth.go:212-214`, `internal/db/users.go:51-54`), and
`IsFacetsEmployee()` additionally gates the internal portfolio panel and the
"may grant admin to an external user" guardrail. So anyone who can register a
domain of the form `facets.<anything>` gets admin on this product.

This is reachable from the public internet whenever `ALLOWED_DOMAINS` is `*`.

### The fix

Introduce **one** helper and make all seven sites call it. Do not fix the
sites individually — the whole point is that the predicate exists once.

**Step 1.** Create a new file `internal/auth/facets_domain.go`:

```go
package auth

import "strings"

// facetsDomains is the exhaustive list of domains whose users are Facets
// employees. Adding a domain here grants its users admin on first login, so
// this list is a security boundary — change it only deliberately.
var facetsDomains = []string{
	"facets.cloud",
	"facets.io",
}

// IsFacetsEmail reports whether email belongs to a Facets-owned domain.
//
// It matches on an exact domain, never on a substring:
// "evil@facets.attacker.com" is NOT a Facets address even though it contains
// the text "@facets.".
func IsFacetsEmail(email string) bool {
	lower := strings.ToLower(strings.TrimSpace(email))
	at := strings.LastIndex(lower, "@")
	if at < 0 || at == len(lower)-1 {
		return false
	}
	domain := lower[at+1:]
	for _, d := range facetsDomains {
		if domain == d {
			return true
		}
	}
	return false
}
```

Note the implementation takes the text **after the last `@`** and compares the
whole domain for equality. Suffix matching alone is not enough either:
`HasSuffix("a@evil-facets.cloud", "facets.cloud")` is true, and that is also a
domain an attacker can register. Equality after the last `@` is the only
correct form.

**Step 2.** Replace each of these call sites:

| file | line (approx) | replace with |
|---|---|---|
| `internal/auth/oauth.go` | 211 | `isFacets := IsFacetsEmail(email)` |
| `internal/auth/oauth.go` | 455 | `isFacets := IsFacetsEmail(email)` |
| `internal/auth/session.go` | 67 | `return IsFacetsEmail(s.Email)` |
| `internal/db/users.go` | 51 | see Step 3 — different package |
| `internal/jobs/admin_handlers.go` | 363 | `isTargetFacets := auth.IsFacetsEmail(body.Email)` |
| `internal/jobs/admin_handlers.go` | 382 | `uFacets := auth.IsFacetsEmail(u.Email)` |
| `internal/db/users_test.go` | 43 | see Step 4 |

In `internal/auth/*.go` the helper is in the same package, so call it bare
(`IsFacetsEmail`). In `internal/jobs/admin_handlers.go` the file already
imports `internal/auth` (it uses `sess.IsFacetsEmployee()`), so call it as
`auth.IsFacetsEmail`.

After replacing `oauth.go:211` and `oauth.go:455`, the local variable
`lowerEmail` may become unused on one or both of those paths — Go will not
compile with an unused variable. Check each site: if `lowerEmail` is still used
further down, leave it; if not, delete its declaration. Same for
`lowerTargetEmail` in `admin_handlers.go` — but note Item 2 below adds a new
use of it, so check that after doing both items.

**Step 3 — `internal/db/users.go`.** Package `db` must not import `internal/auth`
if that would create an import cycle. Check first:

```bash
grep -n '"github.com/facets/cur-web/internal/db"' internal/auth/*.go
```

- If that prints **nothing**, there is no cycle: add the `auth` import to
  `users.go` and use `auth.IsFacetsEmail(email)`.
- If it prints **anything**, there is a cycle. In that case move the helper
  into a new leaf package `internal/emaildomain` (same file content, change
  `package auth` to `package emaildomain`), and have both `internal/auth` and
  `internal/db` import that. Update the table in Step 2 accordingly.

**Step 4 — `internal/db/users_test.go:43`.** This test currently *reimplements*
the buggy predicate inline, which means it would keep passing after you fix the
production code — a test that cannot fail is worse than no test. Replace the
inline copy with a call to the real helper, then add this table test in
whichever package the helper ends up in:

```go
func TestIsFacetsEmail_RejectsLookalikeDomains(t *testing.T) {
	cases := []struct {
		email string
		want  bool
	}{
		{"arnav@facets.cloud", true},
		{"ARNAV@FACETS.CLOUD", true},
		{"someone@facets.io", true},
		// Lookalikes an attacker can register — all must be false.
		{"evil@facets.attacker.com", false},
		{"evil@facets.evil.ru", false},
		{"evil@evil-facets.cloud", false},
		{"evil@facets.cloud.phish.io", false},
		{"facets.cloud@gmail.com", false},
		{"no-at-sign", false},
		{"trailing@", false},
		{"", false},
	}
	for _, c := range cases {
		if got := IsFacetsEmail(c.email); got != c.want {
			t.Errorf("IsFacetsEmail(%q) = %v, want %v", c.email, got, c.want)
		}
	}
}
```

### Done when

- `grep -rn 'Contains(.*"@facets\.' --include=*.go .` prints **nothing**.
- `grep -rn 'HasSuffix(.*"@facets\.' --include=*.go .` prints nothing (the new
  helper uses equality, not `HasSuffix`).
- `TestIsFacetsEmail_RejectsLookalikeDomains` passes.
- `go build ./... && go test ./...` clean.

---

## Item 2 — `ADMIN_EMAILS` break-glass can be silently cancelled by a stored demotion

### The bug

`ADMIN_EMAILS` is an environment-variable allow-list, read in
`internal/config/config.go:254`. It is the break-glass path: it is what gets
you back into the admin panel when the database says you are a viewer.
`internal/db/users_test.go:77` (`TestAdminEmailsOverridesStoredViewerRole`)
asserts that `sess.IsAdmin(adminEmails)` is true even when the stored role is
`viewer`, and that assertion passes today.

The hole is in the **writer**, not the reader. `AdminSetUserRole` in
`internal/jobs/admin_handlers.go` lets one admin set another user's stored role
to `viewer` with no check against `ADMIN_EMAILS`. The config list still wins at
read time, so the demotion does not actually remove access — it just creates a
permanent, invisible disagreement between the database and the environment.
The admin UI then shows that person as a viewer while they continue to hold
admin, which is exactly the kind of lie that makes an access review useless.

The "last facets admin" guardrail at `admin_handlers.go:372-391` does not cover
this: it counts `@facets.` admins in the database and is blind to `ADMIN_EMAILS`.

### The fix

Reject the demotion outright and say why.

`h.adminEmails` is already available on the handler — it is passed in
`cmd/server/main.go:110` via `jobs.NewHandler(..., cfg.AdminEmails)`. Confirm
the exact field name before using it:

```bash
grep -rn 'adminEmails\|AdminEmails' internal/jobs/*.go
```

In `AdminSetUserRole`, immediately **after** the `currentRole` block
(around line 360, before the `isTargetFacets` line) insert:

```go
	// Guardrail 7: an address in $ADMIN_EMAILS resolves as admin at read time
	// regardless of its stored role, so storing "viewer" for it would not
	// revoke anything — it would only make the admin UI disagree with reality.
	// Refuse, and tell the operator where the real change has to happen.
	if body.Role != "admin" {
		for _, ae := range h.adminEmails {
			if strings.EqualFold(strings.TrimSpace(ae), lowerTargetEmail) {
				http.Error(w, `{"error":"this address is in the ADMIN_EMAILS environment allow-list and resolves as admin regardless of stored role; remove it from ADMIN_EMAILS and restart the server to revoke access"}`, http.StatusConflict)
				return
			}
		}
	}
```

`409 Conflict` is the right status: the request is well-formed and the caller is
authorized, but it conflicts with the server's configured state.

### Frontend

Find where the role dropdown surfaces errors:

```bash
grep -rn 'set-role\|setUserRole\|role' frontend/src/ | grep -i admin
```

The 409 body's `error` string must be shown to the operator verbatim — it is the
only place that tells them to edit the env var. If the current code shows only a
generic "failed to update role", fix it to render `body.error` when present.

### Test

Add to `internal/jobs/admin_panel_testplan_test.go` (the file from Item 0):

```go
func TestAdminSetUserRole_RefusesDemotingAdminEmailsEntry(t *testing.T) {
	// Construct the handler with adminEmails containing the target address,
	// stored role "admin", then POST role="viewer".
	// Assert: status 409, and the response body mentions ADMIN_EMAILS.
	// Assert: the stored role is UNCHANGED afterwards (read it back).
}
```

Follow the construction pattern already used by the other tests in that file —
do not invent a new harness.

### Done when

- Demoting an `ADMIN_EMAILS` address returns 409 and leaves the stored role unchanged.
- Demoting any other admin still works exactly as before.
- The new test passes; all existing tests still pass.

---

## Item 4 — remove the legacy `/api/facets/jobs/{id}/logs` alias

### The bug

`cmd/server/main.go:316` registers:

```go
r.Get("/api/facets/jobs/{id}/logs", jobHandler.FacetsLogs)
```

and `internal/jobs/facets.go:87-89` documents `FacetsLogs` as "a
backward-compatible alias for JobLogs, kept for one release." That release has
shipped. Two routes reaching the same handler is a standing hazard: every
future authorization change has to be remembered twice, and the `/api/facets/`
prefix invites the assumption that the route is internal-only when its
authorization is in fact whatever `JobLogs` does.

### The fix — in this order

1. **Prove the frontend no longer calls it.**

   ```bash
   grep -rn 'api/facets' frontend/src/
   grep -rn 'api/facets' --include=*.go --include=*.ts --include=*.tsx --include=*.js .
   ```

   If **any** non-test caller remains, stop and fix that caller to use the
   canonical path first (find it with
   `grep -n 'jobHandler.JobLogs' cmd/server/main.go`), then come back.

2. Delete the route line `cmd/server/main.go:316`.

3. Delete the `FacetsLogs` function from `internal/jobs/facets.go` entirely
   (the whole func plus its doc comment). Do not leave a deprecated stub —
   a stub nobody calls is the same hazard with extra steps.

4. Delete or re-point any test that referenced it:
   `grep -rn 'FacetsLogs' --include=*_test.go .`

### Done when

- `grep -rn 'FacetsLogs' --include=*.go .` prints nothing.
- `grep -rn 'api/facets' .` prints nothing outside documentation.
- `go build ./... && go test ./...` clean.
- Manual check: the job-logs panel in the UI still loads for a job you own.

---

## Item 5 — assumptions have no category, so the admin UI cannot group them

### The bug

`internal/db/db.go` contains exactly **one** occurrence of the string
`category`, which means the assumptions table has no category column and the
admin assumptions screen renders one flat, unordered list. With the licensing
and ratio work landing, that list grows past the point where an operator can
find anything in it, and "which assumptions affect compute sizing?" becomes
unanswerable without reading every row.

### The fix

**Step 1 — schema.** Find the assumptions table definition:

```bash
grep -n 'assumptions' internal/db/db.go | head -30
```

Add a `category TEXT NOT NULL DEFAULT 'other'` column. Follow this repo's
existing migration convention — look at how a previous column was added
(`grep -rn 'ALTER TABLE' internal/db/`). If migrations are idempotent
`ALTER TABLE ... ADD COLUMN` guarded by a check, do the same; if they are
numbered files, add the next one. **Do not** change the `CREATE TABLE` only —
that would leave every already-deployed database without the column.

**Step 2 — categories.** Use exactly this closed set, storing the lowercase key:

| key | covers |
|---|---|
| `compute_sizing` | vCPU:RAM ratios, machine-family defaults, instance-shape assumptions |
| `licensing` | OS licence fractions, BYOL handling, licence-tier bands |
| `storage` | disk-type mapping, IOPS/throughput assumptions |
| `networking` | NAT VM-count assumptions, egress tiering |
| `commitment` | CUD/SP coverage and term assumptions |
| `pricing_policy` | OD-vs-OD comparison basis, currency, catalog vintage |
| `other` | anything not yet classified — the default |

Define them once in Go (a `var AssumptionCategories = []string{...}` next to the
assumptions model) and have the API serve that list to the frontend rather than
the frontend keeping its own copy.

**Step 3 — backfill.** Write the backfill as part of the migration, assigning
each existing assumption key to a category by its name. Read the current keys
first:

```bash
grep -rn 'assumption' skill/aws-gcp-cost-projection/scripts/config_loader.py | head -20
```

Anything you cannot confidently classify stays `other`. Do not guess — an
assumption filed under the wrong category is harder to find than one in `other`.

**Step 4 — write path.** Every place that creates or updates an assumption must
set the category and must reject a category outside the closed set with 400.
Find them: `grep -rn 'Assumption' internal/jobs/*.go internal/db/*.go`.

**Step 5 — API + UI.** The assumptions list endpoint returns `category` on each
row. The admin assumptions screen groups rows under category headings in the
table order above, with `other` last. Each group is collapsible and shows its
row count in the heading.

**Step 6 — report.** The per-job `assumptions.json` snapshot and the assumptions
section of the rendered report carry the category too, so the customer-facing
report groups them the same way the admin panel does. Find the snapshot writer
with `grep -rn 'assumptions.json' skill/aws-gcp-cost-projection/scripts/`.

### Done when

- A fresh database and an already-populated database both end up with the column.
- Every existing assumption row has a non-empty category.
- Creating an assumption with `category: "nonsense"` returns 400.
- The admin screen shows grouped, collapsible sections with counts.
- The report's assumptions section is grouped identically.
- Tests cover: migration idempotency (run it twice), invalid-category rejection,
  and that the Go category list and the frontend rendering agree.

---

## Final verification for the whole work order

```bash
go build ./...
go test ./...
cd skill/aws-gcp-cost-projection && python3 -m pytest tests/ -q
```

Expected: all Go packages pass; the Python suite passes at its current count
(1,130 as of this writing — higher is fine, lower is not).

Then confirm each of these prints nothing:

```bash
grep -rn 'Contains(.*"@facets\.' --include=*.go .
grep -rn 'FacetsLogs' --include=*.go .
git status --porcelain internal/jobs/
```

## Commit

One commit per item, message form:

```
fix(auth): match Facets domains exactly, not by substring

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

Do not squash Item 1 with the others — it should be reviewable on its own as a
security fix.

---

## Out of scope — do not start these

`WORKORDER_passthrough_to_mapped.md` (7 tasks: ratio registry, Recalculate
endpoint, class-C mappers, `passthrough_reason_missing` guard) is a separate,
larger work order and is still entirely unimplemented. It is **not** part of
this one. Finish the five items above first.
