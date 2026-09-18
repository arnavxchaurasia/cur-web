# Phase 2 — Shared Principles

Every service-specific prompt file in this directory builds on these. Read this first.

---

## Dynamic candidate selection — the core pattern

**Never pick from a hardcoded list.** Instead, for every row:

1. **Query all candidates** from `gcp_sku_rates` for the target service + region, ordered by price ascending
2. **Apply performance floor** — filter out candidates that would degrade performance (see service file for per-service rules)
3. **Check availability** — for each candidate top-to-bottom, verify it exists in the target region via `find-sku.sh` or GCP Billing Catalog API
4. **Pick the first available** that passes the floor
5. If none pass → `strategy='passthrough'` with explanation

This loop guarantees you always get the cheapest real option, not a hardcoded guess. New SKUs GCP adds automatically become candidates next run — no code change needed.

### Candidate query template

```sql
-- Adapt WHERE clauses per service file
SELECT gcp_sku_id, description, resource_group, rate_usd, pricing_type
FROM gcp_sku_rates
WHERE gcp_service = '<service>'
  AND region      = '<row.gcp_region>'
  AND pricing_type = 'OnDemand'
  AND rate_usd    > 0
ORDER BY rate_usd ASC;
```

### Availability check

```bash
# Returns SKU row if available in that region, empty if not
bash scripts/find-sku.sh --service "<gcp_service>" --region "<gcp_region>" --sku-id "<gcp_sku_id>"
```

For live verification beyond the bundled catalog, call the GCP Billing Catalog API:
```
GET https://cloudbilling.googleapis.com/v1/services/{serviceId}/skus?currencyCode=USD
```
Filter the response by `serviceRegions` containing the target region.

---

## Output format (same for every service)

Write to `projection-audit/mappings/<mechanic_group>_mappings.json` — a JSON array:

```json
[
  {
    "aws_li_key":        "abc123",
    "gcp_service":       "Compute Engine",
    "gcp_sku_id":        "6F81-5844-456A",
    "gcp_sku_name":      "N2D AMD Instance Core running in Americas",
    "component":         "core",
    "strategy":          "map",
    "unit_multiplier":   16,
    "gcp_region":        "us-east4",
    "projection_note":   "N2D AMD cheapest sustained option in region; C4A not available",
    "mapping_confidence": 0.92
  }
]
```

`break_down` rows (compute, DB) emit multiple entries per `aws_li_key` — one per component.

---

## Bill is ground truth

| Use — visible in bill | Never use — invisible |
|---|---|
| Explicit discount lines (EDP, RI-applied, SP) | Speculative reseller absorption |
| Credits and refunds | "Probably free tier" without a cited URL |
| Multi-AZ / HA in description text | "Production-sized → must be HA" |
| Instance type from operation field | Customer's "probable" ISA dependency |

---

## 60/40 rule — price/performance

After the performance floor filter leaves multiple valid candidates:
- **60% weight price, 40% weight performance** — cheaper wins unless the perf delta is material
- "Slight perf benefit" is not a reason to pick a pricier option
- "AWS row explicitly says Multi-AZ / HA" is
- Never downgrade tier (Zonal ≠ Regional, burstable ≠ sustained, Standard ≠ SSD)

---

## Back-check (mandatory, rule #9)

After picking a SKU, compute the projected cost and verify:

Load back-check thresholds from `data/review-config.json`:

**Branch A — `aws_amortized_cost > backcheck_branch_a_aws_floor_usd`**: ratio `gcp_projected / aws_amortized` must be between `backcheck_ratio_min` and `backcheck_ratio_max`. Outside → multiplier is wrong. Fix multiplier, not the narrative.

**Branch B — `aws_amortized_cost ≤ backcheck_branch_a_aws_floor_usd`**: `gcp_projected` must also be ≤ `backcheck_branch_b_gcp_ceiling_usd`. Larger → multiplier is wrong.

If both multiplier interpretations fail → `strategy='passthrough'`.

---

## Passthrough rules

**Never passthrough these** (always has a GCP target): EC2, EBS, RDS/Aurora, ElastiCache, S3, Data Transfer, ELB/ALB/NLB, Lambda, Route 53, KMS, CloudWatch.

**Valid passthrough**: No GCP equivalent at all (AWS Support, Marketplace SaaS, AWS-only features with no GCP analog) OR back-check fails on both multiplier interpretations.

**Passthrough budget**: Before signaling done, sum passthrough `aws_amortized_cost` ÷ slice total. Must be < `passthrough_budget_max_fraction` (load from `data/review-config.json`). Exceeding this means you gave up on mappable rows.

`projection_note` for passthrough must state which reason applies: "No GCP equivalent" or "Unit irreconcilable".

---

## mapping-notes.md journal (append-only)

For every non-obvious pick, append:

```markdown
## <aws_li_key> — <short row description>
- Picked: <gcp_sku_id> <sku-name>  (total hourly: $X)
- Candidates swept: <N> SKUs in region; top 3 by price: [A, B, C]
- Alt rejected: <sku-id> +X% cost, <why not>
- Open: <anything to verify — multiplier derivation, assumption made>
```
