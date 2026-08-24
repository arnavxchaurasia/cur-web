# Phase 3 — Review

**Run by:** one **fresh** sub-agent — not one of the mapping agents.
**Reads:** `review_flags.md` + `review_candidates.json` (written by `auto_review.py` before this phase).
**Writes:** `review_fixes.json` — the script applies all DB changes; the agent never touches the DB directly for flagged rows.
**Do NOT:** re-run Phase 2 scripts, do broad `SELECT *` queries, or write SQL UPDATEs for rows listed in `review_flags.md`.

---

## How it works

`auto_review.py` already ran. It detected two categories of problems and wrote:
- `review_flags.md` — human-readable, one section per flagged row with a pre-computed candidate where available
- `review_candidates.json` — machine-readable candidates for `apply_review_fixes.py` to consume

Your job: read every flagged row in `review_flags.md` and write `review_fixes.json` — a JSON array of decisions. Then the script applies them.

---

## Validation checklist — MANDATORY before writing any override

Read `data/validation-rules.json` at the start of this phase. Every `override` decision must pass all BLOCK-severity rules before you write it to `review_fixes.json`. Do not skip rules because a candidate "looks right" — run each check explicitly.

### Rule summary (full detail in `data/validation-rules.json`)

| Rule ID | Check | How |
|---|---|---|
| `no_hallucinated_sku_format` | gcp_sku_id matches `[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}` | Visual check — never invent an ID |
| `sku_exists_in_region` | SKU exists AND covers the row's `gcp_region` | `bash scripts/find-sku.sh "<gcp_service>" "<gcp_region>"` |
| `service_sku_consistency` | SKU's service (from find-sku.sh output) matches `gcp_service` you write | find-sku.sh output shows the owning service |
| `no_intentional_passthrough_override` | Row's `projection_note` does NOT contain "no GCP equivalent" / "no GCS equivalent" / "no direct GCP equivalent" | Read the note in `review_flags.md` |
| `tier_safety_sustained_to_burstable` | Non-t-family EC2 source (m5, c5, r6g, …) must NOT map to E2 or T2A Arm SKU | Check usage_type + gcp_sku_name |
| `multiplier_positive` | unit_multiplier > 0 | Arithmetic check |
| `multiplier_dimensional_correctness` | `core` rows use vCPU count; `ram` rows use RAM GiB | Cross-reference `instance_vcpus`/`instance_ram_gb` in `aws_li_catalog` |
| `multiplier_sanity_bounds` | vCPU ≤ 512, RAM ≤ 8192 GiB (WARN, not BLOCK) | If exceeded, verify against spec and note "Verified against instance spec" in reason |

### GCP/AWS API knowledge sources

When you need to verify a SKU's existence, region coverage, or pricing, consult:
- **GCP Billing Catalog API:** `https://cloud.google.com/billing/docs/reference/rest/v1/services.skus/list` — lists all real SKU IDs by service and region
- **GCP Compute Engine pricing:** `https://cloud.google.com/compute/vm-instance-pricing`
- **GCP Cloud SQL pricing:** `https://cloud.google.com/sql/pricing`
- **GCP Memorystore pricing:** `https://cloud.google.com/memorystore/docs/redis/pricing`
- **GCP BigQuery pricing:** `https://cloud.google.com/bigquery/pricing`
- **AWS EC2 instance specs:** `https://aws.amazon.com/ec2/instance-types/`
- **AWS ElastiCache node specs:** `https://aws.amazon.com/elasticache/pricing/`

The full list with all API URLs is in `data/validation-rules.json` under `gcp_api_knowledge_sources`.

**If any BLOCK rule fails for an override → use `veto` instead, document the failure in `reason`.**

---

## Step 1 — Write `review_fixes.json`

For **every** flagged row in `review_flags.md`, emit one entry:

```json
[
  {
    "aws_li_key": "abc123",
    "decision": "confirm",
    "reason": "candidate looks correct — EC2 m5.xlarge → N2 Custom Core"
  },
  {
    "aws_li_key": "def456",
    "decision": "override",
    "gcp_sku_id": "XXXX-YYYY-ZZZZ",
    "gcp_sku_name": "N2 Instance Core running in us-east4",
    "gcp_service": "Compute Engine",
    "reason": "candidate SKU was wrong region — found correct one via find-sku.sh"
  },
  {
    "aws_li_key": "ghi789",
    "decision": "veto",
    "reason": "AWS Support charge — no GCP equivalent, passthrough is correct"
  }
]
```

**Decision rules:**

| Decision | When to use |
|---|---|
| `confirm` | Confidence is `HIGH` and the pre-computed candidate looks correct — just accept it |
| `override` | Confidence is `LOW` (no candidate found) or the candidate is wrong — provide your own `gcp_sku_id` + `gcp_sku_name`, or a corrected `unit_multiplier` + `component` |
| `veto` | Row is a genuine passthrough (AWS Support, Marketplace, no real GCP equivalent) — document why in `reason` |

**Flag types and how to handle each:**

| Flag type | What it means | Action |
|---|---|---|
| **Passthrough requiring review** | strategy=passthrough, but no "no GCP equivalent" stamp — may have a real mapping | `confirm` pre-computed candidate, or `override`/`veto` |
| **Spec violation** | break_down row with wrong unit_multiplier vs. instance spec | `confirm` HIGH-confidence candidate (correct vCPU/RAM from spec) |
| **No SKU** | strategy=map/break_down but gcp_sku_id is NULL — rate-fill will produce $0 | `override` with correct gcp_sku_id found via `find-sku.sh`, or `veto` if should be passthrough |
| **Zero multiplier** | unit_multiplier = 0 or NULL — GCP cost = 0 regardless of rate | `override` with the correct numeric multiplier (vCPUs for core, RAM GiB for ram) |

For **spec violations**, all candidates are `HIGH` — `confirm` them unless the spec values look wrong. Do not invent multiplier values; correct values come from `instance_vcpus`/`instance_ram_gb` in `aws_li_catalog`.

For **LOW confidence passthrough rows** and **no-SKU rows**, look up the correct SKU:
```bash
bash scripts/find-sku.sh "<gcp_service>" "<region>"
```
Then use `override` with the found `gcp_sku_id`. If genuinely no GCP equivalent exists, `veto` and explain.

---

## Step 2 — Apply the fixes

Once `review_fixes.json` is written, run:

```bash
python3 scripts/apply_review_fixes.py
```

The script reads `review_fixes.json` + `review_candidates.json` and applies DB changes. It runs `llm_override_guard.py` as a final format gate (SKU ID regex, multiplier sign/type) — business-logic rules are your responsibility to enforce at Step 1 via the validation checklist above. Out-of-scope overrides (rows not in `review_candidates.json`) are rejected automatically.

---

## Step 3 — `Alt:` and `Open:` lines in `mapping-notes.md` (secondary, direct SQL)

Only do this after Step 2 is complete. These are NOT handled by the script pipeline — fix them via direct SQL UPDATE.

### `Alt:` lines — was the trade-off honest?
For each `## <aws_li_key>` entry with an `Alt:` line:
- Did the picked SKU win the 60/40 trade-off honestly?
- If the rationale has no line-item anchor from the bill, **demote to the alternative**.
- UPDATE `aws_li_to_gcp_li` directly.

### `Open:` lines — resolve the uncertainty
A `fail-by-Nx` or `degenerate` back-check is a real signal. Cross-reference other rows in the bill to confirm or fix the unit_multiplier. Fix with a direct UPDATE.

Append a brief justification to `mapping-notes.md` under `## Review findings`.

---

## Returning to main

One paragraph. Concrete numbers. Example:

> Confirmed 6 HIGH-confidence candidates, overrode 2 LOW-confidence passthroughs (found correct SKUs via find-sku.sh), vetoed 1 (AWS Support — no GCP equivalent). `apply_review_fixes.py` applied 8 fixes. Fixed 1 Alt: demotion and 1 Open: multiplier via direct SQL. Ready for Phase 4.
