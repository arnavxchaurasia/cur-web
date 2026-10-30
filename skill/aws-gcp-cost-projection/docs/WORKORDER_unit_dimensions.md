# WORK ORDER — Replace unit "classes" with dimension vectors

**For an implementing AI with no prior context. Read fully before editing.**
Paths are relative to `skill/aws-gcp-cost-projection/`.

**Scope: the unit-compatibility guard only.** You are not changing which SKU is
chosen, only whether a chosen SKU is *allowed* on dimensional grounds. Expect
some rows to newly fail the guard — that is the point — but every such row must
be explained (§5).

Audit finding: **D-11** in `docs/AUDIT_pipeline_defects.md`.

---

## 1. The problem, in five steps

1. `scripts/resolve_sku.py:338-347` is the guard that stops a per-hour SKU
   being used to price a per-GB row. Its docstring says it
   *"rejects dimensional mismatches as passthrough (G-1b)"*. It is the defence
   against the historical 50x / 120x / 1300x bugs.

2. It compares two labels from `unit_equivalence.unit_class()`, which has five
   possible answers: `time`, `storage-month`, `data`, `count`, `seat`.

3. `unit_class()` ends in substring heuristics
   (`scripts/unit_equivalence.py:70-78`): anything containing `sec`, `hour`,
   `hrs` or `hr` returns `time`.

4. **Most cloud units are products of two dimensions**, and they all collapse
   to the time component. Measured against the live catalog
   (`data/catalog.duckdb`, 20 distinct `usage_unit` values):

   | GCP unit | SKUs | `unit_class()` | actually |
   |---|---:|---|---|
   | `GiBy.h` | **12,761** | `time` | data x time |
   | `GiBy.mo` | 9,112 | `storage-month` | data x time |
   | `mo` | 3,574 | **None** | time |
   | `GBy.h` | 600 | `time` | data x time |
   | `s` | 316 | **None** | time |
   | `yr` | 313 | **None** | time |
   | `GiBy.s` | 274 | `data` | data x time |
   | `GiBy.d` | 246 | `data` | data x time |
   | `TiBy` | 116 | **None** | data |
   | `MiBy` | 49 | **None** | data |

   **Half of the catalog's unit vocabulary is wrong or unknown.** AWS units
   fare no better: `Lambda-GB-Second`, `vCPU-Hours`, `ACU-Hrs`, `GiB-Hours`
   and `Shard-Hours` all return `time`; `IOPS-Mo` and `vCPU-Months` return
   `None`.

5. Two failure modes result, both of which let a bad match through:

   - **Compound collapses to time.** `GiBy.h` and `Hrs` are both "time", so a
     memory-GiB-hour SKU is judged compatible with an instance-hour row. A
     GiB-hour rate applied to an hour quantity is wrong by the machine's GiB
     count — 4x, 16x, 64x.
   - **Unknown disables the check.** `if res_cls is not None and exp_cls is not
     None` means an unparsed unit skips the guard rather than failing it.

   `GiBy.h` is the third most common unit in the catalog and covers the Cloud
   SQL, Memorystore and GKE Autopilot memory SKUs — exactly the families this
   pipeline maps most.

### Worked example

```
AWS row   : RDS db.m5.xlarge instance-hours, pricing_unit = "Hrs", 730 hours
GCP SKU   : "Cloud SQL for MySQL: Memory", usage_unit = "GiBy.h", $0.0063

unit_class("Hrs")    -> time
unit_class("GiBy.h") -> time        <-- same class, guard PASSES

Priced as : 730 x 0.0063            = $4.60
Correct   : 730 x 16 GiB x 0.0063   = $73.58     (16x under)
```

The guard that exists to catch exactly this says the units match.

---

## 2. The fix: model a unit as dimensions plus a scale

The class set is too coarse. Replace it with two facts per unit:

- a **dimension vector** — which base quantities it multiplies
- a **scale** — its size relative to the canonical unit of those dimensions

Two units are compatible **iff their dimension vectors are equal**. The scale
difference then becomes an explicit conversion factor instead of something
`unit_multiplier` absorbs silently.

Base dimensions: `time`, `data`, `compute`, `memory`, `count`, `iops`, `seat`.
Canonical units: hour, GiB, vCPU, GiB, 1, IOPS, 1.

This matters because **`GB-Mo` and `GB-Second` have the same dimension vector**
`{data:1, time:1}` and differ only by scale — a factor of 2.6 million. The
current model cannot express that at all; dimensions plus scale can.

### The vocabulary is small — about 30 entries

The catalog has 20 distinct units and the bills add roughly 10. This is a
table, not a parser. Write it as data in `data/unit_dimensions.json`:

```jsonc
{
  "_meta": { "canonical": { "time": "h", "data": "GiBy", "compute": "vCPU" } },
  "units": {
    "h":                { "dims": {"time": 1}, "scale": 1 },
    "s":                { "dims": {"time": 1}, "scale": 0.000277778 },
    "min":              { "dims": {"time": 1}, "scale": 0.0166667 },
    "d":                { "dims": {"time": 1}, "scale": 24 },
    "mo":               { "dims": {"time": 1}, "scale": 730 },
    "yr":               { "dims": {"time": 1}, "scale": 8760 },
    "GiBy":             { "dims": {"data": 1}, "scale": 1 },
    "MiBy":             { "dims": {"data": 1}, "scale": 0.000976563 },
    "TiBy":             { "dims": {"data": 1}, "scale": 1024 },
    "GBy":              { "dims": {"data": 1}, "scale": 0.931323 },
    "GiBy.h":           { "dims": {"data": 1, "time": 1}, "scale": 1 },
    "GiBy.mo":          { "dims": {"data": 1, "time": 1}, "scale": 730 },
    "GiBy.s":           { "dims": {"data": 1, "time": 1}, "scale": 0.000277778 },
    "GiBy.d":           { "dims": {"data": 1, "time": 1}, "scale": 24 },
    "GBy.h":            { "dims": {"data": 1, "time": 1}, "scale": 0.931323 },
    "count":            { "dims": {"count": 1}, "scale": 1 },

    "Hrs":              { "dims": {"time": 1}, "scale": 1 },
    "GB-Mo":            { "dims": {"data": 1, "time": 1}, "scale": 679.866 },
    "GB":               { "dims": {"data": 1}, "scale": 0.931323 },
    "Requests":         { "dims": {"count": 1}, "scale": 1 },
    "Lambda-GB-Second": { "dims": {"data": 1, "time": 1}, "scale": 0.000258701 },
    "vCPU-Hours":       { "dims": {"compute": 1, "time": 1}, "scale": 1 },
    "ACU-Hrs":          { "dims": {"acu": 1, "time": 1}, "scale": 1 },
    "IOPS-Mo":          { "dims": {"iops": 1, "time": 1}, "scale": 730 }
  }
}
```

**Derive the scales rather than trusting mine** — show your arithmetic in the
summary. `GB-Mo` is `0.931323 GiB x 730 h`; `Lambda-GB-Second` is
`0.931323 GiB x (1/3600) h`. If you disagree with a number, say so and justify
it; do not silently substitute.

---

## 3. Tasks

### T1 — `data/unit_dimensions.json`

Build the table. Cover **every** distinct unit actually present:

```bash
python -c "
import duckdb
c=duckdb.connect('data/catalog.duckdb',read_only=True)
for u,n in c.execute('SELECT usage_unit, count(*) FROM skus GROUP BY 1 ORDER BY 2 DESC').fetchall():
    print(n, repr(u))"
```

and the AWS side from the corpus (`SELECT DISTINCT pricing_unit FROM
aws_li_catalog`). Any unit you cannot assign dimensions to with confidence:
**leave it out and list it in your summary.** An absent entry becomes
`unknown`, which now fails loudly — that is the correct outcome, not a reason
to guess.

### T2 — Rewrite `scripts/unit_equivalence.py`

Keep the module and the `unit_class()` name as a thin shim so existing callers
do not break, but add the real API:

```python
def unit_dims(unit) -> dict | None      # {"data":1,"time":1}, or None if unknown
def unit_scale(unit) -> float | None
def compatible(a, b) -> tuple[bool, str]        # (ok, reason)
def conversion_factor(a, b) -> float | None     # scale_a / scale_b, or None
```

**Delete the substring heuristics.** They are the cause. A unit is either in
the table or it is unknown — no guessing from a fragment.

Normalise before lookup: trim, case-fold, and treat `-`, `_`, ` ` and `.` as
the same separator, so `GiBy.h`, `giby-h` and `GiB Hours` reach one entry. Keep
that normalisation in one function with its own test.

### T3 — Invert the guard's default in `resolve_sku.py`

```python
# before: unknown units skip the check
if res_cls is not None and exp_cls is not None:
    if res_cls != exp_cls: reject

# after: unknown units FAIL the check
ok, why = compatible(resolved_unit, exp_unit)
if not ok:
    reject(f"[unit-mismatch: {why}]")
```

`compatible()` returns `(False, "unknown unit 'X'")` when either side is
missing from the table. **An unparsed unit is precisely when the pipeline
should not be pricing.**

### T4 — Report what the new guard rejects

Before/after on the corpus, using `scripts/diagnostics.py`: emit
`unit_unknown` (unit absent from the table) and `unit_mismatch` (dimensions
differ) with row counts and dollars, so the first run produces a work list
rather than a wall of rejections.

### T5 — Tests, `tests/test_unit_dimensions.py`

1. `test_every_catalog_unit_is_known` — every `usage_unit` in
   `data/catalog.duckdb` resolves, or is on an explicit documented exception
   list. Pin the list; it is a ratchet.
2. `test_compound_units_are_not_time` — `GiBy.h`, `GBy.h`, `GiBy.s`,
   `GiBy.d`, `Lambda-GB-Second`, `vCPU-Hours` all have 2-dimension vectors and
   are **not** compatible with `h` / `Hrs`.
3. `test_same_dims_different_scale` — `GB-Mo` and `Lambda-GB-Second` are
   compatible (same dims) and `conversion_factor` between them is ~2.6e6.
4. `test_unknown_fails_closed` — `compatible("Hrs", "widgets-per-furlong")`
   returns `(False, ...)`, never `(True, ...)`.
5. `test_normalisation` — `GiBy.h`, `giby-h`, `GiBy h` reach the same entry.
6. **The worked example from §1** as a regression test: `Hrs` vs `GiBy.h` must
   be incompatible.

---

## 4. Verification

```bash
cd skill/aws-gcp-cost-projection
python -m pytest tests/ -q
python -m py_compile scripts/unit_equivalence.py scripts/resolve_sku.py
```

Coverage check — paste the output:

```bash
python -c "
import duckdb,sys; sys.path.insert(0,'scripts')
from unit_equivalence import unit_dims
c=duckdb.connect('data/catalog.duckdb',read_only=True)
rows=c.execute('SELECT usage_unit, count(*) FROM skus GROUP BY 1 ORDER BY 2 DESC').fetchall()
unk=[(u,n) for u,n in rows if unit_dims(u) is None]
print(f'distinct units {len(rows)}; unknown {len(unk)}')
for u,n in unk: print('  UNKNOWN', repr(u), n)"
```

Target: **zero unknown**, or every remaining one listed and justified.

---

## 5. Expected and intended changes

State these in your summary rather than treating them as regressions:

- **Some rows will newly fail the guard and become passthrough.** That is the
  fix working: they were being priced against a dimensionally wrong SKU.
- **For each newly-rejected row group, report the AWS unit, the GCP unit, the
  row count and the dollars**, and confirm the rejection is correct. A
  rejection you cannot justify is a bug in the table, not a win.
- **Dollar totals will move.** Unlike previous work orders, "nothing changed"
  is a *failure* signal here — it would mean the guard still is not firing.
  Report the before/after totals and attribute the difference.

---

## 6. Do NOT

- Do **not** keep the substring heuristics "as a fallback". They are the bug.
- Do **not** invent dimensions for a unit you do not understand — leave it out.
- Do **not** make `unknown` pass, under any circumstance.
- Do **not** change SKU selection, mapper logic, or `unit_multiplier` values.
  This work order decides *allowed or not*, nothing else.
- Do **not** silently use my scale numbers if you believe one is wrong — say so
  and show the arithmetic.

---

## 7. Done

- [x] `data/unit_dimensions.json` covers every catalog and corpus unit, or
      lists the exceptions. (All 20 distinct catalog units over 90,335 SKUs
      resolve — enforced by `test_every_catalog_unit_is_known`.)
- [x] Substring heuristics deleted; `unit_class()` kept only as a shim.
- [x] `compatible()` fails closed on unknown units.
- [x] `resolve_sku.py` uses it and no longer skips on `None`.
- [x] `unit_unknown` / `unit_mismatch` diagnostics emitted with dollars.
- [x] 6 tests pass, including the `Hrs` vs `GiBy.h` regression case. (Now 7 —
      added `test_every_mapper_expected_unit_is_known`.)
- [ ] Before/after dollar totals reported, with every moved row group explained.
      PARTIAL: see review-fix log — no populated job DB available here to run a
      real before/after; static analysis shows every catalog unit AND every
      mapper expected-unit resolves, so no *legitimate* pairing is newly rejected.

---

## 8. PROGRESS / REVIEW LOG
- 2026-10-30 Review-fix pass — Implementation verified against the real catalog:
  `test_every_catalog_unit_is_known` runs over `data/catalog.duckdb` (90,335 SKUs,
  20 distinct units) and all resolve; fail-closed `compatible()` + `resolve_sku`
  reject-on-mismatch with `unit_unknown`/`unit_mismatch` dollar diagnostics are in
  place; substring heuristics removed, `unit_class()` is a shim. Added
  `test_every_mapper_expected_unit_is_known` pinning the mapper-side vocabulary
  (`infer_expected_unit` outputs + the legacy `storage-month` literal) — all
  resolve, so a legitimate mapping cannot be rejected for lack of a dimension
  entry. Full suite 1142 -> 1143 (new test) passing.
  STILL OPEN: the before/after DOLLAR impact of the stricter guard (which real
  row groups moved priced -> passthrough) is unevidenced — needs a run against a
  populated job DB. Static reasoning says movement is limited to genuine
  dimensional mismatches the old lenient code wrongly accepted (correct moves),
  but this must be confirmed on a real bill before treating §5 as satisfied.
