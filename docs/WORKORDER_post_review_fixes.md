# WORK ORDER — Five fixes found reviewing the per-user/per-job work

**For an implementing AI with no prior context. Read §0 and §1 first.**

Five independent fixes. **Item 1 is a security fix — do it first.** The rest
can be done in any order.

None of these change how a cost projection is calculated. If you find yourself
editing a rate, a mapping or a SQL sum, stop — you are outside this work order.

Baselines before you start: **all Go packages passing, 1,130 Python tests,
`tsc -b` clean.** Record them; they must not regress.

---

## 0. Context

This app estimates GCP cost from an AWS bill. Admin access is restricted to
Facets employees, except where a Facets admin has explicitly granted admin to
an external user. That grant mechanism is new, which makes the employee check
load-bearing in a way it was not before — hence item 1.

---

## 1. Rules

1. **Default closed.** When a check is ambiguous, the safer answer wins.
2. **No endpoint may grant a permission to its caller.**
3. **"Not yours" returns 404**, identical to "does not exist".
4. **A behaviour change gets a test that fails before the fix.**

---

## ITEM 1 — The Facets employee check matches far too much (SECURITY)

### The problem

Six places decide whether an email belongs to a Facets employee, and all six
use the same loose test:

```go
strings.HasSuffix(e, "@facets.cloud") ||
strings.HasSuffix(e, "@facets.io")   ||
strings.Contains(e, "@facets.")          // <-- this one
```

That third clause is a **substring** match. These all pass:

```
alice@facets.cloud          legitimate
bob@facets.io               legitimate
evil@facets.attacker.com    NOT legitimate -- contains "@facets."
x@facets.co.uk.evil.net     NOT legitimate
```

An attacker who controls any domain starting `facets.` is treated as an
employee. Today that means: they can be made admin without a Facets admin's
involvement, they count toward "is the caller a Facets admin" when granting
admin to others, and they satisfy the "last Facets admin" protection.

Before the external-grant feature this was a latent flaw. **Now it is the
check standing between an outside account and admin.**

### Where it lives

```
internal/auth/session.go        IsFacetsEmployee
internal/auth/oauth.go
internal/db/users.go
internal/jobs/admin_handlers.go  (AdminSetUserRole — twice)
```

Six occurrences, four files, all duplicated by copy-paste.

### The fix

1. **One function, one place.** Put the canonical implementation in
   `internal/auth` (extend `IsFacetsEmployee`), export a helper usable from
   `internal/db` and `internal/jobs`, and **delete the other five copies**.
   Duplicated security logic drifts; that is how five of them ended up
   identical but separate.

2. **Suffix match only, against an explicit list:**

```go
var facetsDomains = []string{"facets.cloud", "facets.io"}

func isFacetsEmail(email string) bool {
    e := strings.ToLower(strings.TrimSpace(email))
    at := strings.LastIndex(e, "@")
    if at < 0 { return false }
    domain := e[at+1:]
    for _, d := range facetsDomains {
        if domain == d { return true }
    }
    return false
}
```

Match the domain **exactly**, after the **last** `@`. Not a suffix of the whole
string (that would let `x@evil.com@facets.cloud` through in some parsers), not
a `Contains`.

3. **If a subdomain like `eng.facets.cloud` is genuinely in use**, add
   `strings.HasSuffix(domain, ".facets.cloud")` as a separate, deliberate
   clause — and say so in a comment. Do not reintroduce `Contains` to cover it.

### Tests (write these first; they must fail before the fix)
1. `alice@facets.cloud` → true; `bob@facets.io` → true
2. **`evil@facets.attacker.com` → false**
3. `x@facets.co.uk.evil.net` → false
4. `alice@FACETS.CLOUD` → true (case-insensitive)
5. `no-at-sign` → false; `""` → false
6. `a@facets.cloud@evil.com` → false
7. An end-to-end test: a user on a `facets.`-prefixed attacker domain cannot
   be granted admin and does not count as a Facets admin when granting.

---

## ITEM 2 — Demoting a user listed in `ADMIN_EMAILS` silently does nothing

### The problem

`Session.IsAdmin()` checks the `ADMIN_EMAILS` environment list **before** it
looks at the stored role:

```go
email := strings.ToLower(s.Email)
for _, a := range adminEmails {
    if a == email { return true }    // wins over everything below
}
if s.Role != "admin" { return false }
```

So demoting an env-listed user to `viewer` in the admin UI **saves
successfully and changes nothing**. They keep full admin.

This is a change from the previous ordering, where `Role == "viewer"` returned
false before the env list was consulted.

### The decision to make

Both behaviours are defensible:

- **env list wins** — a bootstrap escape hatch that cannot be locked out by a
  bad UI action
- **role wins** — the admin UI means what it says

**Pick one and make it visible.** The defect is not the ordering; it is that
the UI offers an action that silently does nothing.

### Recommended fix

Keep the env list authoritative (it is the recovery mechanism), but **stop
pretending the UI can override it**:

1. In `AdminSetUserRole`, if the target email is in `ADMIN_EMAILS`, reject any
   change to a non-admin role with: *"This user's admin access is set by
   server configuration (ADMIN_EMAILS) and cannot be changed here."*
2. In the users list, mark such users — e.g. a "config admin" pill — so it is
   obvious before anyone clicks.

### Tests
1. demoting an `ADMIN_EMAILS` user → refused with that message; role unchanged
2. demoting a non-env admin → still works
3. the users list marks env-listed admins distinctly

---

## ITEM 3 — Diagnostics are always stored under run id `"initial"`

`internal/jobs/watcher.go:233`:

```go
w.db.SaveJobDiagnostics(jobID, "initial", summary)
```

The run id is hardcoded. A job can have several runs — the regenerate-report
feature creates them — and `job_diagnostics` is keyed `(job_id, run_id)`, so
every run overwrites the same row.

Today this is harmless: regenerating only re-renders and produces no new
pipeline diagnostics. **It stops being harmless the moment anything re-runs
pricing or mapping**, which is exactly when you would most want to compare two
runs' diagnostics.

### Fix
Pass the real run id. The run identifier already exists — `resolveReportPath`
and `QueryRunResults` work in terms of it; use the same value. If no run id is
available at that point, keep `"initial"` **and leave a comment saying why**,
rather than leaving a bare literal that reads like an oversight.

### Tests
1. two runs of one job produce two `job_diagnostics` rows, not one overwritten
2. fetching diagnostics for a specific run returns that run's data

---

## ITEM 4 — Remove the legacy logs route

`cmd/server/main.go` still registers:

```go
r.Get("/api/facets/jobs/{id}/logs", jobHandler.FacetsLogs)
```

`FacetsLogs` now just calls `JobLogs`, so it is **not** a security hole — it is
owner-scoped like everything else. It was kept as a one-release alias and
carries a `TODO(cleanup)`.

### Fix
1. Confirm nothing in `frontend/src/` still calls `/api/facets/jobs/*/logs`
   (grep it).
2. If nothing does, delete the route and the `FacetsLogs` wrapper.
3. If something does, point it at `/api/jobs/{id}/logs` first, then delete.

**Do not skip step 1.** Deleting a route the UI still calls turns a working
page into a silent 404.

### Test
Frontend builds and the logs viewer still works after removal.

---

## ITEM 5 — Separate AWS assumptions from GCP conversion assumptions in the UI

### The problem

The assumptions page lists all eight values in one flat table:

```
aurora_acu_vcpu            Aurora ACU to vCPU          (GCP conversion)
aurora_acu_ram_gib         Aurora ACU to RAM           (GCP conversion)
glue_dpu_vcpu / _ram_gib   Glue DPU to vCPU/RAM        (GCP conversion)
lambda_mb_per_vcpu         Lambda MB to vCPU ratio     (GCP conversion)
looker_studio_pro_seat_usd Looker Studio Pro seat      (GCP price)
aws_sp_discount_pct        AWS Savings Plans Discount  (AWS side)
aws_spot_discount_pct      AWS Spot Discount           (AWS side)
```

Nothing tells a reader which side of the comparison a value affects. A real
question this already caused: *"GCP CUDs differ per service — why is one shown
in assumptions?"* The answer is that no GCP CUD is there at all; GCP CUDs live
per-service in `data/cud_pct.json`. The two discount rows are **AWS**-side.

An admin who misreads which side a number affects will change the wrong one
and move the estimate in the wrong direction.

### Fix

1. Add a `category` to each assumption — `aws_input`, `gcp_conversion`,
   `gcp_price` — seeded alongside the existing fields. Follow the pattern
   already used by `report_settings.group_name`.
2. Group the UI under clear headings:
   - **AWS inputs** — "what we assume about the AWS bill"
   - **AWS → GCP conversion** — "how an AWS unit maps to a GCP unit"
   - **GCP prices** — "GCP list prices we hold directly"
3. On the two discount rows, state in the rationale that the value is a **flat
   approximation**: real Savings Plan discounts vary by family, term and
   payment option (roughly 20–60%), and Spot varies more. They are a disclosed
   assumption used where the bill format does not give gross cost — say that,
   rather than leaving "published typical".

### Not in scope
Making SP/Spot discounts per-service or per-family. That is a modelling change
needing its own decision. **Note it in your summary; do not build it.**

### Tests
1. every assumption has a category, and the ratchet test that every key is read
   by a script still passes
2. the UI renders all three groups with every assumption under exactly one

---

## 2. Verification

```bash
cd /c/Users/arnav/Downloads/cur-web-AGY && go build ./... && go test ./internal/...
cd skill/aws-gcp-cost-projection && python -m pytest tests/ -q
cd frontend && npm run build
```

- Go packages all pass; Python at **1,130** plus your new tests; `tsc -b` clean.
- **A projection's numbers are unchanged.** Render a report before and after
  and compare the headline totals.
- For items 1 and 2, confirm each new test **failed before** your fix. A
  security test that passed immediately is usually not testing what you think.

---

## 3. Do NOT

- Do **not** leave any `Contains(email, "@facets.")` anywhere (Item 1).
- Do **not** keep five copies of the employee check — one function, one place.
- Do **not** silently change the `ADMIN_EMAILS` precedence without making the
  UI reflect it (Item 2).
- Do **not** delete the legacy logs route before grepping the frontend
  (Item 4).
- Do **not** change SP/Spot to per-service modelling (Item 5 scope note).
- Do **not** change any pricing, mapping or total.

---

## 4. Done

- [ ] Item 1: one `isFacetsEmail`, exact-domain match, five duplicates deleted,
      7 tests that failed first
- [ ] Item 2: `ADMIN_EMAILS` precedence made explicit in API and UI; 3 tests
- [ ] Item 3: real run id in `SaveJobDiagnostics`; 2 tests
- [ ] Item 4: legacy route removed after confirming no caller
- [ ] Item 5: assumptions categorised and grouped; rationales corrected
- [ ] All suites green; projection totals unchanged
- [ ] Summary says what shipped, what the Item 2 decision was and why, and
      anything noticed but not fixed
