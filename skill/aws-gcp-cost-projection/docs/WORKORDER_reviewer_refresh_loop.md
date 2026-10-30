# WORKORDER — Advisory reviewer + self-healing refresh loop (accuracy capstone)

**For the implementation agent. Read fully. Verify status in Phase 0.**
This closes the loop so accuracy *compounds* across bills instead of being
re-fixed by hand each time (see memory `project_refresh_and_reviewer_layer`).
It is **read-only / advisory** — it never silently rewrites prices. It flags
specific, structured findings to an admin route and persists learned facts.

---

## 0. ORIENTATION — run first, every time

| # | Probe (from `skill/aws-gcp-cost-projection/`) | Tells you | RESULT |
|---|---|---|---|
| P1 | `ls scripts/ \| grep -i "review\|refresh\|learn"` | what review/refresh scaffolding already exists (auto_review.py, apply_review_fixes.py, weekly_catalog_refresh.py, learned-product-codes.json seen) | |
| P2 | `grep -rn "learned" data/ scripts/ \| head` | which learned-fact stores exist and who writes them | |
| P3 | `grep -rn "admin" internal/ scripts/ \| grep -i "review\|finding\|flag" \| head` | is there an admin route to receive findings? | |
| P4 | `python -m pytest tests/ -q 2>&1 \| tail -3` | green baseline | |

Resume rule: much scaffolding may already exist (`auto_review.py`,
`apply_review_fixes.py`, `weekly_catalog_refresh.py`,
`data/learned-product-codes.json`). Determine what each already does BEFORE adding
anything — this work order may be mostly wiring, not greenfield.

---

## 1. GOAL — two cooperating pieces

**(A) Advisory reviewer** — after a projection runs, a read-only pass (LLM-assisted
but gated) emits **structured findings**, not prose:
```
{ job_id, aws_li_key, canonical_service, charge_type,
  finding_type: one of {missing_spec, suspect_ratio, target_not_in_allowed,
                         passthrough_should_be_mapped, unit_mismatch, stale_rate},
  evidence: { aws_od, gcp_od, ratio, rule_or_spec_hit, ... },
  severity, suggested_action }
```
Findings go to an **admin route** for a human to accept/reject. The reviewer
**never edits prices directly.**

**(B) Self-healing refresh store** — accepted findings (and LLM-learned service
facts: new product_codes, confirmed SKU mappings, confirmed rates) persist to a
versioned **refresh file**, so the next bill benefits automatically. Promotion from
"LLM-guessed this once" to "trusted fact" requires either repeated agreement or
admin acceptance — never a single unverified guess.

---

## 2. INVARIANTS
- Reviewer is READ-ONLY on the projection. It writes findings + (on acceptance)
  the refresh store; it does not mutate `gcp_projection`.
- A learned fact is only promoted to trusted after confirmation (admin accept OR
  N independent agreements). Record provenance + confirmation count per fact.
- Findings are structured rows, never free prose — the admin UI and the
  refresh-promotion logic both consume them programmatically.
- Everything the reviewer flags must point at a specific row + specific evidence
  ($-weighted). No "looks off generally."

---

## 3. PHASES

### Phase 1 — Structured findings schema + reviewer pass
- [ ] Define the findings schema (above) as a table + JSON contract.
- [ ] Build/extend the reviewer to emit findings from signals you ALREADY compute:
      spec_gap, ratio outliers (post OD-vs-OD), target-not-in-allowed-enum,
      passthrough-on-unit-compatible-service, stale catalog vintage.
- [ ] Reviewer runs read-only after projection; writes findings only.

### Phase 2 — Admin route to triage findings
- [ ] Surface findings in the admin panel grouped by service + severity + $.
- [ ] Accept / reject / defer per finding; accept writes to the refresh store.

### Phase 3 — Refresh store + promotion
- [ ] Versioned refresh file: learned product_codes, confirmed SKU mappings,
      confirmed rates, with provenance + confirmation count.
- [ ] Pipeline reads the refresh store (after seeded data, before LLM) so trusted
      learned facts short-circuit the LLM next run.
- [ ] Promotion rule: untrusted → trusted on admin accept OR N agreements.

### Phase 4 — Close the loop
- [ ] Confirm a service fixed via a finding stays fixed on the next bill WITHOUT
      another manual edit (the whole point). Demonstrate on one real service.

---

## 4. PROGRESS LOG (append-only, newest at bottom)
- 2026-10-30 Phase 0 — work order created. Scaffolding suspected present
  (auto_review.py, apply_review_fixes.py, weekly_catalog_refresh.py,
  learned-product-codes.json). NEXT: run P1–P4, map what exists to these phases,
  then fill only the gaps.

## 5. IF STUCK
Prefer surfacing a finding over auto-applying a fix. A wrong auto-applied price is
silent corruption; a flagged finding a human ignored is merely a missed
improvement. Bias the whole system toward advisory.
