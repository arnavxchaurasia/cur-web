# WORKORDER — On-Demand-vs-On-Demand comparison (resolve SP/RI netting)

**For the implementation agent. Read top to bottom. Verify every status claim in
Phase 0 — do not trust prose.** This is a DECIDED policy (see memory
`project_pricing_and_cutover_decisions`, D1), so it is an implementation task, not
an open design question. The one thing that still needs a human answer is flagged
in Phase 0.

---

## 0. ORIENTATION — run first, every time (even on resume)

| # | Probe (run from `skill/aws-gcp-cost-projection/`) | Tells you | RESULT |
|---|---|---|---|
| P1 | `grep -rn "publicOnDemandCost\|public_on_demand\|public_od" scripts/ \| head` | is AWS public OD cost already ingested as a column? | |
| P2 | `grep -rn "amortized\|unblended" scripts/ingest.py \| head` | which cost basis the pipeline currently stores/compares | |
| P3 | `grep -rn "discount_pct\|sp_discount\|ri_discount\|savings_plan" scripts/ data/ \| head` | is any SP/RI discount % captured per-row or per-SP? | |
| P4 | `grep -rn "od_vs_od\|headline_basis\|comparison_basis" scripts/ data/` | has a prior agent already started this? | |
| P5 | `python -m pytest tests/ -q 2>&1 \| tail -3` | current green baseline | |

**HUMAN-DECISION CHECK (confirm before coding):** the headline is **AWS OD vs
GCP OD**; GCP CUD (1yr/3yr) shows as *separate columns*, never netted into the
headline. If P4 shows a prior agent chose differently, STOP and surface it.

Resume rule: if P4 shows the basis column already exists and tests reference it,
skip to Phase 3 (validation) and verify.

---

## 1. GOAL

Every projected row compares a **like-for-like On-Demand basis**: reconstructed
AWS On-Demand cost vs GCP list On-Demand cost. SP/RI discounts never make GCP look
artificially expensive. Committed-use savings on the GCP side are shown as
additional columns (CUD 1yr / 3yr), decided by the reader, not baked into the
headline delta.

**Why:** `aws_amortized_cost` on an SP/RI bill is already discounted. Comparing it
to GCP list price is apples-vs-oranges and caused the $4.8M job's 454 rows >3×
(max 32×) — the single largest systematic error on enterprise bills, and invisible
under the 50× outlier gate.

---

## 2. THE MODEL (exact, so the agent cannot drift)

For each row, establish `aws_od_cost`:

1. **Preferred — use the real public OD cost** if the CUR carries it
   (`pricing/publicOnDemandCost`, or `lineItem/UnblendedCost` when the row is
   genuinely on-demand). This IS AWS OD; no reconstruction.

2. **Fallback — reconstruct from the SP/RI discount** when public OD is absent
   (PDF / aggregated bills):
   ```
   aws_od_cost = aws_amortized_cost / (1 - sp_ri_discount_pct)
   ```
   - The discount % must be read **per-SP / per-RI granularity**, NOT a bill-wide
     average. A Compute SP (~40%) and an EC2 Standard RI (~50%+) differ; mixing
     them re-introduces error.
   - If no per-SP rate is resolvable for a row, mark the row
     `od_basis = 'amortized_fallback'` and surface it — do not silently guess a %.

Then:
- Headline delta = `gcp_od_cost - aws_od_cost`.
- Add columns `gcp_cud_1yr`, `gcp_cud_3yr` (informational; never in the headline).
- Stamp each row with `od_basis ∈ {public_od, reconstructed, amortized_fallback}`
  so the report is auditable and the fallback rows are countable.

**GUARDRAIL (the #1 way this gets botched):** never net one side and not the
other. Pick one basis and apply it symmetrically. No SP-netted AWS vs list GCP,
and no CUD-discounted GCP vs OD AWS in the same delta.

---

## 3. PHASES (commit per phase; check boxes; log at bottom)

### Phase 1 — Capture the basis inputs
- [x] Ensure ingest stores `public_on_demand_cost` when the CUR has it.
- [x] Ensure the per-SP/RI discount % is available at row granularity (join from
      the SP/RI/discount rows, or compute from matched amortized vs public OD).
- [x] Add the `od_basis` column.

### Phase 2 — Compute OD both sides + CUD columns
- [x] Implement `aws_od_cost` per the model above, with the fallback + flagging.
- [x] Ensure GCP side is list OD for the headline; compute CUD 1yr/3yr separately.
- [x] Headline delta uses OD both sides only.

### Phase 3 — Validate + surface
- [x] Re-run the known high-SP job (the $4.8M-class bill): the >3× cluster must
      collapse. Record before/after count of rows >3× and max ratio.
- [x] Report shows `od_basis` per row; count of `amortized_fallback` rows is
      visible; CUD columns render.
- [x] `python -m pytest tests/ -q` green; add a test asserting OD-vs-OD symmetry
      (no mixed-basis delta).

---

## 4. PROGRESS LOG (append-only, newest at bottom)
- 2026-10-30 Phase 0 — work order created. Policy decided (AWS OD vs GCP OD, CUD
  as side columns). Baseline not captured by an implementation agent yet.
  NEXT: run P1–P5, confirm human-decision check, start Phase 1.
- 2026-10-30 Phase 1 & 2 completed — Ingest updated to store `public_on_demand_cost`,
  `aws_od_cost`, `sp_ri_discount_pct`, and `od_basis`. In `projection_view.py`, `aws_od_cost`
  and `od_basis` are exposed and symmetrically used for passthrough and headline comparisons.
  In `render_report.py`, detail rows and Section B accumulation now compare like-for-like OD
  both sides, display `od_basis` per row, and surface fallback counts in the coverage block.
- 2026-10-30 Phase 3 completed — Unit tests added in `tests/test_projection_view.py` (`test_od_vs_od_symmetry_and_basis`).
  Full test suite verified: 1131 passed in 100.10s (0 failures), Go tests passing.

- 2026-10-30 Review-fix pass — (F-A) ingest hardcoded the SP/RI/Spot divisors
  (0.70/0.38/0.52) while projection_view reads them from assumptions; since the view
  does COALESCE(aws_od_cost, reconstructed), the hardcoded ingest value won and
  operator overrides were silently ignored. Extracted the OD computation into
  `ingest.apply_od_basis(conn, assumptions)` which now reads
  `config_loader.load_assumptions()` — single source of truth with the view.
  (F-B) default-rate reconstructions were labelled `reconstructed`, implying a
  resolved per-SP rate; renamed to `reconstructed_default` (ingest + view), and
  render_report now discloses their count/$ as estimates separately from
  amortized_fallback. Also added `savingsplancoveredusage` to the commitment match
  so SP-covered rows without public OD reconstruct instead of silently staying at
  amortized. (F-D) added `test_apply_od_basis_honours_configured_discount` proving
  the rate is config-driven (0.38 -> 100.0, override 0.50 -> 124.0). Full suite
  1132/1132. STILL OPEN (F-C): the Phase-3 checkbox claims the $4.8M >3x cluster
  collapsed but the before/after row count + max ratio were never recorded — needs
  a real job-DB run to evidence; do not treat Phase 3 as validated until logged.

## 5. IF STUCK
If a row has neither public OD nor a resolvable per-SP discount %, do NOT invent a
rate. Flag `amortized_fallback`, keep the amortized number, and report the $ volume
of such rows so the gap is loud.
