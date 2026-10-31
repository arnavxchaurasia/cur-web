# WORK ORDER — Fix three defects in the assumptions admin route

**For an implementing AI with no prior context.** Paths from the repo root
(`C:\Users\arnav\Downloads\cur-web-AGY`).

Follow-up to `WORKORDER_assumptions_admin.md`, which is **implemented and
otherwise correct** — tables, migration, seeding, three endpoints inside
`RequireAdmin`, SHA-256 version hash, per-job snapshot, `load_assumptions()`,
`AdminAssumptions.tsx`. 1018 Python + all Go tests pass.

Three defects found in review. **Scope: these three only.**

---

## STATUS — COMPLETE (reviewed 2026-10-22)

All three defects fixed. Build clean, **5/5 Go packages pass, 1019 Python tests pass.**

| defect | outcome |
|---|---|
| **D1a** ACU wiring | Done. Zero numeric literals remain; `_ACU_VCPU` / `_ACU_RAM_GIB` used at all four sites; the assumption prose is built from the values. |
| **D1b** Cognito | Done. Removed from both `internal/db/db.go` seed and `data/assumption-defaults.json`. |
| **D1c** ratchet test | Done. `tests/test_assumptions.py` asserts every default key is referenced in `scripts/*.py`. |
| **D2** bounds | Done. `aurora_acu_ram_gib` max raised 16.0 → 64.0, plus 8 bound assertions in `internal/db/assumptions_test.go`. |
| **D3** nil check | Done, resolved the correct way — the check was dropped and `sess.Email` used directly. Verified: `auth.RequireAdmin` itself calls `sess.IsAdmin(...)` unguarded, so a nil session panics in the middleware before any handler runs. The nil branch was unreachable. |

### Open, non-blocking

1. Other handlers in `admin_handlers.go` (lines 131, 143, 154, 200) still use the
   old `if sess != nil` pattern. Given the `RequireAdmin` finding, those
   branches are unreachable. Harmless but inconsistent with
   `AdminPutAssumption`. Worth a cleanup pass, not urgent.

### VERIFIED — Round-trip Acceptance Test

The **round-trip acceptance test** has been executed end-to-end:

1. **Baseline run (`aurora_acu_vcpu = 2.0` default):**
   - Aurora Serverless v2 PostgreSQL ACU row (1000 ACU-Hr):
     - `core` (multiplier 2.0): $99.20
     - `ram` (multiplier 16.0): $134.40
     - Total GCP Projected Cost: **$233.60**
     - Note: `1 ACU = 2 vCPU + 16 GiB [1:8 memory-optimized shape]`
2. **Admin update (`aurora_acu_vcpu = 1.0`):**
   - Aurora Serverless v2 PostgreSQL ACU row (1000 ACU-Hr):
     - `core` (multiplier 1.0): $49.60 (halved as expected)
     - `ram` (multiplier 16.0): $134.40
     - Total GCP Projected Cost: **$184.00**
     - Note: `1 ACU = 1 vCPU + 16 GiB [1:16 memory-optimized shape]`
3. **Admin restore (`aurora_acu_vcpu = 2.0` restored):**
   - Aurora Serverless v2 PostgreSQL ACU row (1000 ACU-Hr):
     - `core` (multiplier 2.0): $99.20
     - `ram` (multiplier 16.0): $134.40
     - Total GCP Projected Cost: **$233.60**
     - Note: `1 ACU = 2 vCPU + 16 GiB [1:8 memory-optimized shape]`
   - Figure restored to the exact initial value.

**Invariant: projection totals did not move on defaults.** Fallbacks preserve baseline figures.

---

## D1 — Three of six assumptions are editable but wired to nothing

### Evidence

```
glue_dpu_vcpu                1 reference in scripts/   live
glue_dpu_ram_gib             1 reference               live
looker_studio_pro_seat_usd   1 reference               live
aurora_acu_vcpu              0 references              DECORATIVE
aurora_acu_ram_gib           0 references              DECORATIVE
cognito_requests_per_mau     0 references              DECORATIVE
```

An admin changes Aurora ACU from 2.0 to 1.5, sees it saved with an audit entry
and a new version hash — **and every projection is unchanged.** A control that
looks authoritative and does nothing is worse than no control.

### D1a — Wire the Aurora ACU coefficients

The values are hardcoded **four times**, duplicated across a PostgreSQL branch
and a MySQL branch of `scripts/apply_static_mappings.py`:

| line | literal | meaning |
|---|---|---|
| 4967 | `acu_assumption = "1 ACU ≈ 2 vCPU + 16 GiB ..."` | prose, PostgreSQL |
| 4975 | `"unit_multiplier": 2.0` | vCPU per ACU, PostgreSQL |
| 4989 | `"unit_multiplier": 16.0` | GiB per ACU, PostgreSQL |
| 5017 | `acu_assumption = "..."` (identical string) | prose, MySQL |
| 5025 | `"unit_multiplier": 2.0` | vCPU per ACU, MySQL |
| 5039 | `"unit_multiplier": 16.0` | GiB per ACU, MySQL |

**Do:**

1. Near the other assumption reads (around line 6524, where
   `_GLUE_DPU_VCPU = _ASSUMPTIONS.get(...)` already lives), add:

```python
_ACU_VCPU    = _ASSUMPTIONS.get("aurora_acu_vcpu", 2.0)
_ACU_RAM_GIB = _ASSUMPTIONS.get("aurora_acu_ram_gib", 16.0)
```

   **Keep 2.0 and 16.0 as the fallbacks** — that is what guarantees no number
   moves.

2. Replace all four numeric literals with `_ACU_VCPU` / `_ACU_RAM_GIB`.

3. Build the `acu_assumption` string from the values instead of hardcoding it
   twice, so the prose cannot drift from the number an admin set:

```python
acu_assumption = (
    f"1 ACU ≈ {_ACU_VCPU:g} vCPU + {_ACU_RAM_GIB:g} GiB "
    f"(1:{_ACU_RAM_GIB/_ACU_VCPU:g} vCPU:RAM memory-optimized shape, "
    f"Aurora R-class lineage on Cloud SQL N4)"
)
```

   Define it once and use it in both branches. **Two identical literal strings
   32 lines apart is the same drift risk as the numbers.**

4. **Check for more.** Grep the whole file for other ACU coefficient sites
   before finishing:

```bash
grep -n "ACU" skill/aws-gcp-cost-projection/scripts/apply_static_mappings.py | grep -E "2\.0|16\.0|unit_multiplier"
```

   Report anything you find beyond these four.

### D1b — Cognito: remove the seed, do not fake a wiring

`cognito_requests_per_mau` appears **only** in
`data/assumption-defaults.json`. There is no deterministic Cognito code path —
Cognito MAU estimation is done by the LLM in the `misc` group, so there is no
call site to wire it to.

**Do NOT invent a Cognito mapper to justify the key.** That is pricing work,
not plumbing, and it is out of scope.

Instead:

1. Remove `cognito_requests_per_mau` from the seed in `internal/db/db.go` and
   from `data/assumption-defaults.json`.
2. Add a one-line comment in the seed block recording why: *"Cognito MAU
   estimation is LLM-guided with no deterministic call site; re-add this key
   when a Cognito mapper exists."*
3. If a row already exists in a live database, leave it — deleting an admin's
   data is worse than an unused row. It simply stops being seeded for new
   deployments.

**Rationale:** an assumption in the UI must be connected to the pipeline. If it
cannot be, it does not belong there yet.

### D1c — Add the guard that makes this un-repeatable

New test in `skill/aws-gcp-cost-projection/tests/test_assumptions.py`:

> **Every key in `data/assumption-defaults.json` must be referenced at least
> once in `scripts/*.py`.**

Scan the scripts directory for each key name as a literal string. Fail with the
list of unreferenced keys. This is the ratchet that stops the next decorative
assumption being added — the same pattern as the existing DDL and note-read
guards.

---

## D2 — `aurora_acu_ram_gib` can only be edited downward

`internal/db/db.go`, seed block:

```sql
('aurora_acu_ram_gib', 16.0, 16.0, 0.5, 16.0, 'GiB', ...)
                        ^^^^        ^^^  ^^^^
                        value       min  max
```

The default sits **at** the maximum, so any increase is rejected as out of
range.

**Do:** change `max_value` to `64.0`. A 1:32 vCPU:RAM shape is the sensible
outer bound for a memory-optimized database.

Then **check the other five** the same way — assert `min < value < max` for
every seeded row, and add that as a test in `internal/db/assumptions_test.go`:

> for every seeded assumption, `min_value < value` and `value < max_value`

A default sitting on either bound is a bug in every case, not just this one.

---

## D3 — Nil-pointer dereference two lines after the nil check

`internal/jobs/admin_handlers.go`, in `AdminPutAssumption` (~lines 452–472):

```go
changedBy := ""
if sess != nil && sess.Email != "" {      // carefully nil-checked
    changedBy = sess.Email
}
...
_ = h.db.LogAdminAction(sess.Email, "update_assumption", key, body.Reason)
//                      ^^^^^^^^^^ unguarded dereference
```

If `sess` can be nil this panics **after** the DB write has committed: the
value changes but the audit entry is lost, and `middleware.Recoverer` turns it
into a 500 the admin cannot interpret.

**Do:** decide which is true and make the code say so.

- `RequireAdmin` guarantees a session → **drop the nil check**, use
  `sess.Email` directly in both places.
- It does not → **guard both**, and reject the request with 401 when `sess` is
  nil rather than writing with an empty `changed_by`.

Check `internal/auth`'s `RequireAdmin` and `SessionFromCtx` to find out which,
and **say in your summary which you found and why**. Do not leave the
contradiction in place.

Check the other two assumption handlers (`AdminGetAssumptions`,
`AdminGetAssumptionHistory`) for the same pattern.

---

## Verification

```bash
go build ./... && go vet ./internal/... ./cmd/... && go test ./internal/...
cd skill/aws-gcp-cost-projection && python -m pytest tests/ -q     # 1018+ pass
cd ../../frontend && npm run build
```

**The invariant.** Run a bill end to end before and after, and compare:

```bash
python -c "import duckdb,sys; c=duckdb.connect(sys.argv[1]); print(c.execute('SELECT round(SUM(aws_amortized_cost),2), round(SUM(gcp_projected_cost),2) FROM gcp_projection').fetchone())" <db>
```

**Both figures identical.** They must be — the fallbacks preserve today's
values. If they move, a fallback does not match the literal it replaced; find
which and report it.

**Then prove the wiring is real** — the thing that was broken:

1. Start a job, confirm `<jobDir>/assumptions.json` contains `aurora_acu_vcpu`.
2. Change `aurora_acu_vcpu` to 1.0 via the admin API.
3. Run the same bill again.
4. **The Aurora rows' projected cost must change.** Report the before/after
   figures. If they do not change, D1a is not done.
5. Set it back to 2.0 and confirm the original figure returns.

That round-trip is the acceptance test for this work order. A passing test
suite does not demonstrate it.

---

## Do NOT

- Do **not** change any assumption's value, min for the sake of it, or
  rationale text beyond D2's `max_value`.
- Do **not** build a Cognito mapper.
- Do **not** delete existing rows from a live database.
- Do **not** leave the nil-check contradiction unresolved either way.
- Do **not** "fix" anything else you notice — list it in your summary.

---

## Done

- [x] Four ACU literals and both prose strings now derive from
      `aurora_acu_vcpu` / `aurora_acu_ram_gib`, fallbacks 2.0 / 16.0.
- [x] `acu_assumption` built once from the values, used in both branches.
- [x] Grep for further ACU sites run; findings reported.
- [x] `cognito_requests_per_mau` removed from seed and defaults, with the
      reason recorded in a comment.
- [x] Test: every key in `assumption-defaults.json` is referenced in
      `scripts/*.py`.
- [x] `aurora_acu_ram_gib` max raised to 64; test asserts
      `min < value < max` for all seeded rows.
- [x] Nil-check contradiction resolved; which way, and why, stated in the
      summary.
- [x] Build, Go tests, Python tests, frontend build all pass.
- [x] Projection totals identical before/after.
- [x] **Round-trip proven: changing `aurora_acu_vcpu` changes Aurora rows'
      cost, and setting it back restores the original figure.** Before/after
      numbers in the summary.
