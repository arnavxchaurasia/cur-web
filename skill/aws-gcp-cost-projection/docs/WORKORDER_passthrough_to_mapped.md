# WORK ORDER — Turn passthrough rows into real projections

**For an implementing AI with no prior context. Read §0, §1 and §2 first.**

Seven tasks. **Do them in the order given** — the early ones are worth more
money and carry less modelling risk than the later ones. Each task is
independently shippable; finish and verify one before starting the next.

Baselines: **all Go packages passing, 1,130 Python tests, `tsc -b` clean.**
Record them. They must not regress.

---

## 0. Background — what a "passthrough" is and why it matters

This pipeline maps AWS bill lines to GCP SKUs. When it cannot price a line, it
marks the row `strategy = "passthrough"` and carries **the AWS cost unchanged**
into the GCP column.

**Passthrough is not neutral.** It silently asserts *"GCP costs exactly what
AWS costs"* — a ratio of precisely 1.0 — for that row. It is an invisible
assumption that can never be noticed or challenged. Every row we convert from
passthrough to a real projection makes the estimate more honest, whether the
new number is higher or lower.

### Measured today (11 real bills, $131,835 of workload spend)

```
passthrough_reason        rows      aws $   services
unclassified               111      9,105   cloudwatch, guardduty, kinesis, msk, s3, securityhub, sqs
unresolved_sku             103      8,134   datatransfer, ec2, elasticache
service_policy_review       34        594   kms, route53, vpc, waf
                           ---    -------
TOTAL                      248     17,833   (13.5% of workload spend)
```

Broken down by charge type, the largest blocks are:

```
ec2    BoxUsage:m7g.* / t4g.*      Hrs        $4,165   Graviton (ARM)
msk    Kafka.m7g.large / .xlarge   Hrs        $4,270   Graviton (ARM)
s3     Requests-Tier1 / Tier2      Requests   $1,969   unit-model mismatch
guardduty PaidEvents / Malware     GB         $1,715   different pricing model
datatransfer Bandwidth             GB-Mo      $1,456   blank usage type
```

**The single biggest cause is ARM, not a missing assumption.** Roughly $8,400
of the $17,833 is Graviton instances (`m7g`, `t4g`, `Kafka.m7g`) that found no
GCP ARM rate in their region. That is a catalog-coverage problem and needs no
modelling decision at all — which is why it is Task 1.

### And 43 services are passthrough by *policy*

`data/service_map.json` has **43 rules with `mode: "review"`**. Those services
passthrough on every bill, whether or not this corpus happened to contain them.
Reading their stated reasons, they fall into three classes:

| class | meaning | examples | what to do |
|---|---|---|---|
| **A — no GCP equivalent** | nothing to map to | Neptune, DocumentDB, Transfer Family, SES, AWS Config | leave as passthrough, but say *why* (Task 2) |
| **B — incompatible pricing model** | a target exists, units differ | CloudTrail, API Gateway, EventBridge, GuardDuty, Inspector, DynamoDB, DMS | needs an assumption (Task 5) |
| **C — named target, simply not built** | the rule already names the GCP service | DAX/MemoryDB -> Memorystore, Secrets Manager -> Secret Manager, Directory Service -> Managed AD, Athena -> BigQuery, SageMaker -> Vertex AI, VPC Endpoint -> PSC | **map it deterministically — no assumption needed** (Task 3) |

**Class C is the largest untapped win and the lowest risk.** The GCP target is
already written in the rule; nobody has implemented the mapper.

---

## 1. Vocabulary you will need

- **`unit_multiplier`** — how many GCP units one AWS unit becomes. If AWS bills
  one NAT gateway-hour and GCP bills per VM-hour, the assumed VM count *is*
  the multiplier. `GCP cost = GCP rate × usage × unit_multiplier`.
- **assumption** — a value we chose because the AWS bill does not state it.
- **ratio group** — two assumptions that only make sense together, e.g.
  `aurora_acu_vcpu` and `aurora_acu_ram_gib`, whose quotient is a machine shape.
- **deterministic chain** — the pipeline stages that need no LLM:
  `classify_mechanics -> apply_commitment_ignores -> apply_static_mappings ->
  merge_mappings -> service_classifier -> apply_rates -> render_report`.
  It runs in seconds.

---

## 2. Rules

1. **Never invent an equivalence.** If GCP does not sell the thing, say so.
   A wrong number is worse than an honest gap.
2. **Every assumed row must say it is assumed**, in the row's note and in the
   report.
3. **A ratio must be one GCP actually sells.** Not a free number.
4. **Totals never change because something was hidden.** Display settings are
   display only.
5. **Default closed.** Out-of-range or missing input falls back to the declared
   default and emits a diagnostic.
6. **Do not change a mapping that already works** to make a new one fit.

---

## TASK 1 — ARM / regional rate coverage (biggest win, no modelling)

**~$8,400 of the $17,833.** Graviton rows (`m7g.*`, `t4g.*`, `Kafka.m7g.*`)
come out `passthrough` with `unresolved_sku`.

The validator's own repair log names the cause:

```
[repair] ... passthrough->map (Amazon Elastic Compute Cloud / T2A Arm core)
[repair] ... passthrough->map (Amazon Elastic Compute Cloud / no GCP rate found in region core)
```

So a SKU *name* resolves but no **rate** exists for it in that region.

### Investigate first, then fix
1. For one failing row, print the resolved SKU description, the region, and
   what `gcp_sku_rates` holds for it. Establish **which** of these it is:
   - the ARM family (T2A/C4A) genuinely is not sold in that region
   - the rate exists but the catalog fetch missed it
   - the region mapping is sending us to a region that has no ARM
2. If the family is genuinely unavailable in-region, the correct answer is a
   **declared** passthrough with reason `family_not_available_in_region` — not
   `unresolved_sku`. Say it plainly in the note.
3. If the catalog is incomplete, add those SKUs to the prefetch
   (`scripts/prefetch_skus.py`) and refresh.
4. If the region mapping is wrong, fix the mapping.

**Report which cause it was.** Do not "fix" it by falling back to x86 rates —
that would quietly price an ARM workload as x86 and understate it.

### Done when
Graviton rows are either priced, or passthrough with an accurate reason. The
`unresolved_sku` bucket shrinks by roughly the ARM total.

---

## TASK 2 — Give every passthrough a real reason

**111 rows / $9,105 are `unclassified`** — the reason enum's "nobody declared
this" value. We cannot prioritise what we cannot name.

### Do
1. For each service in that bucket (cloudwatch, guardduty, kinesis, msk, s3,
   securityhub, sqs), find where the row becomes passthrough and set a reason
   from the closed enum in `data/passthrough_reasons.json`. Add a new enum
   value only if none fits, and document it there.
2. Add a check that **fails loudly** when a passthrough row is written with no
   reason — `diag(WARN, "passthrough_reason_missing", ...)` with the row count
   and dollars. Otherwise this bucket silently refills.

### Done when
`unclassified` is near zero, and any new occurrence raises a diagnostic.

---

## TASK 3 — Map the class-C services (named target, no assumption)

These rules in `data/service_map.json` **already name a GCP service**. They are
passthrough only because no deterministic mapper exists. Build them, highest
AWS spend first — check each against the corpus before building, and skip any
that never appears on a real bill.

| AWS | GCP target named in the rule |
|---|---|
| DAX / DynamoDB Accelerator / MemoryDB | Memorystore for Redis |
| Secrets Manager | Secret Manager (per secret-version + access) |
| Systems Manager / Parameter Store (advanced) | Secret Manager |
| Directory Service / Managed AD | Managed Service for Microsoft AD |
| Certificate Manager | Certificate Manager (usually $0 both sides) |
| Cost Explorer | Cloud Billing (free on GCP — likely a $0 row) |
| Athena | BigQuery (per TB scanned) |
| Glue | Dataflow |
| SageMaker | Vertex AI |
| VPC Endpoint | Private Service Connect |

**For each one:**
1. Confirm the GCP SKU exists in `data/catalog.duckdb` *before* writing the
   mapper. If it does not, stop and report — do not substitute a near-match.
2. Check the units match. Where they do not, it belongs in Task 5, not here.
3. Write the mapper following an existing one of similar shape.
4. Add a test with a realistic row.

**Do not batch these into one commit.** One service per commit, each with its
own test, so a wrong mapping is easy to find and revert.

---

## TASK 4 — A registry of ratios GCP actually sells

Prerequisite for Task 5.

GCP predefined families have fixed vCPU:RAM shapes, already encoded in
`scripts/candidate_pool.py` as `ram_per_vcpu`:

```
C4, C4D, C4A, T2A, N2D  ->  4     (compute-optimised)
N4, N2, E2              ->  8     (balanced)
M3                      -> 16     (memory-optimised)
```

### Build
1. A loader that derives the **allowed set** from that table rather than
   hardcoding `{4, 8, 16}` — adding a GCP family must add an option with no
   code change.
2. Per ratio group, a declared `min_ratio` / `max_ratio` backstop and a
   `default_ratio`.
3. Validation **on write**: reject any admin change whose resulting ratio is
   not in the allowed set, naming both values and the resulting ratio.
4. Validation **on read** (`config_loader`): an out-of-band stored value falls
   back to the default and emits
   `diag(WARN, "assumption_ratio_out_of_band", ...)`. A value edited directly
   in the database must never reach a customer's projection.
5. Admin UI: a **dropdown**, not a number field, labelled meaningfully —
   `1:4 (compute-optimised, C4/N2D)`, `1:8 (balanced, N2/N4/E2)`,
   `1:16 (memory-optimised, M3)`.

### Done when
`aurora_acu_vcpu = 0.1` with `aurora_acu_ram_gib = 64` — a 1:640 shape, both
values inside their individual bounds today — is **rejected**.

---

## TASK 5 — Constrained-ratio estimation for class-B services

For rows where a GCP target exists but AWS never states a dimension GCP needs
(NAT VMs per gateway, MSK broker footprint, ElastiCache node RAM).

### Rules
1. The ratio comes from Task 4's allowed set. Never a free number.
2. **Where AWS documents the dimension, use the document, not a calibration.**
   Aurora's ACU shape is published — keep the documented default.
3. Where nothing is documented, the default is **the allowed ratio whose
   resulting GCP/AWS price ratio is closest to 1.0**. This is defensible
   because the choice set is three real machine shapes, not a free parameter —
   and it replaces a passthrough that already asserted exactly 1.0.
4. Every such row carries a note naming the ratio and that it is an assumption.
5. **Scope:** only rows that are passthrough *for want of a dimension*. A row
   that is passthrough for `no_gcp_equivalent` (Oracle, dense HDD, SES) stays
   passthrough. Conflating these would undo Task 2.

---

## TASK 6 — Show every assumption in the report

A customer must be able to see what we assumed and the exact ratio used.

### Build
A report section listing, for this run: each assumption that actually affected
a row, its value, the ratio used, and one line of plain-English rationale.

- Driven by what the rows **actually used**, not by the full assumption list —
  an assumption that affected nothing should not appear.
- Gated by a `section_assumptions` report setting (it already exists).
- Neutral wording: "we assumed X because the bill does not state Y", not an
  apology.

**This is the section that makes the tool defensible in front of a customer.**
"Here is what we assumed, here is the number, you can challenge it" beats any
claim of accuracy.

---

## TASK 7 — Recalculate without re-running the bill

**Goal: change a ratio, press a button, see the new numbers in seconds.**

Changing an assumption changes `unit_multiplier`, which is written during
mapping. So a pure re-render cannot see it — but a full re-run is not needed
either.

### Build
A **Recalculate** action that re-runs only the **deterministic chain**
(§1), then re-renders. No ingest, no LLM, seconds, no cost.

Model it on the existing regenerate-report endpoint — same admin gating, same
"job directory must still exist" check, same audit entry, and the previous
report stays available.

### The one thing that will bite you
`merge_mappings.py` does `DELETE FROM aws_li_to_gcp_li` and rebuilds the table
from the mapping JSON files on disk. **So the LLM's mapping files must still be
in the job directory**, or a recalculate silently drops every LLM-mapped row
and the totals fall.

**Verify this explicitly before shipping**: run a recalculate on a job that had
LLM-mapped rows and confirm the row count and totals are unchanged when no
assumption was altered. If the files are gone, recalculate must refuse with a
clear message rather than produce a smaller number.

---

## 3. Verification (every task)

```bash
cd /c/Users/arnav/Downloads/cur-web-AGY && go build ./... && go test ./internal/...
cd skill/aws-gcp-cost-projection && python -m pytest tests/ -q
```

Plus, after each task:
- **Re-run the corpus and report the passthrough table from §0.** The row
  count and dollars must fall. State by how much and for which services.
- A bill with no rows of the type you touched must have **identical totals**.
- No row moves from passthrough to mapped without a note explaining the basis.

---

## 4. Do NOT

- Do **not** price ARM rows at x86 rates to make them resolve (Task 1).
- Do **not** leave a passthrough without a declared reason (Task 2).
- Do **not** substitute a near-match SKU when the named target is absent
  (Task 3).
- Do **not** allow a ratio outside the GCP-allowed set (Task 4).
- Do **not** estimate rows that are passthrough for `no_gcp_equivalent`
  (Task 5).
- Do **not** ship Recalculate without proving LLM mappings survive it (Task 7).
- Do **not** change any existing working mapping to make a new one fit.

---

## 5. Done

- [ ] Task 1: Graviton rows priced or accurately reasoned; cause reported
- [ ] Task 2: `unclassified` near zero; a missing reason raises a diagnostic
- [ ] Task 3: class-C services mapped, one commit each, SKU confirmed first
- [ ] Task 4: allowed-ratio registry; 1:640 rejected; dropdown in admin
- [ ] Task 5: constrained estimation, documented values preferred, scoped
- [ ] Task 6: report lists the assumptions actually used, with ratios
- [ ] Task 7: Recalculate re-runs the deterministic chain only, LLM mappings
      proven to survive
- [ ] Passthrough total falls from **248 rows / $17,833**; report the new
      number after each task
