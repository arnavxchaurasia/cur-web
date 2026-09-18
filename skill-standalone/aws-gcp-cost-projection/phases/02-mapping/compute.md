# Phase 2 — Compute mapping (EC2 → Compute Engine)

Handles `mechanic_group = 'compute_breakdown'`. Read `_shared.md` first.

---

## Context extraction

For each row, read these fields from the manifest:

```
aws_li_key         — row identifier
gcp_region         — target GCP region (pre-mapped by Phase 1)
instance_type      — e.g. m5.4xlarge (use this, not operation string)
instance_vcpus     — from aws_li_catalog (Phase 1 populated this)
instance_ram_gb    — from aws_li_catalog
instance_arch      — 'x86_64' or 'arm64'
workload_class     — 'sustained', 'burstable', 'gpu', 'outlier'
pricing_model      — 'OnDemand', 'Spot', 'Committed'
deployment_option  — for HA signal (not used for compute, but read it)
usage_quantity     — hours billed this period (e.g. 720.0 for a full month)
aws_amortized_cost — for back-check
```

If `instance_vcpus` or `instance_ram_gb` is NULL, query the DB:
```sql
SELECT instance_vcpus, instance_ram_gb, instance_arch
FROM aws_li_catalog
WHERE aws_li_key = '<key>';
```
If still NULL, derive from instance_type size token (see size table at bottom) and flag `Open:` in mapping-notes.md.

---

## Output shape

`break_down` — always emit two rows per source: `component='core'` and `component='ram'`.

| component | unit_multiplier | SKU family |
|---|---|---|
| core | `instance_vcpus` | Instance Core SKU |
| ram  | `instance_ram_gb` | Instance Ram SKU |

---

## Dynamic candidate sweep

### Step 1 — get all paired families in the target region

```sql
SELECT
  c.gcp_sku_id                        AS core_sku_id,
  c.description                        AS core_desc,
  r.gcp_sku_id                        AS ram_sku_id,
  r.description                        AS ram_desc,
  c.rate_usd                           AS core_rate,
  r.rate_usd                           AS ram_rate,
  (c.rate_usd * {vcpu} + r.rate_usd * {ram_gb}) AS total_hourly_usd
FROM gcp_sku_rates c
JOIN gcp_sku_rates r
  ON  c.gcp_service    = r.gcp_service
  AND c.region         = r.region
  AND c.pricing_type   = r.pricing_type
  AND c.resource_group = 'CPU'
  AND r.resource_group = 'RAM'
  -- Pair families by stripping the " Instance Core/Ram running in X" suffix
  AND regexp_replace(c.description, '\s+(Predefined\s+)?Instance\s+(Core|Arm\s+Instance\s+Core).*', '')
    = regexp_replace(r.description, '\s+(Predefined\s+)?Instance\s+(Ram|Arm\s+Instance\s+Ram).*', '')
WHERE c.gcp_service  = 'Compute Engine'
  AND c.region       = '{gcp_region}'
  AND c.pricing_type = 'OnDemand'
  AND c.rate_usd     > 0
  AND r.rate_usd     > 0
  AND c.description NOT ILIKE '%preemptible%'
  AND c.description NOT ILIKE '%spot%'
ORDER BY total_hourly_usd ASC;
```

This returns every paired family in the region ordered by total instance cost — no hardcoded family names.

### Step 2 — apply performance floor filter

Remove candidates that would degrade performance below what the AWS source pays for:

**Burstable source:** check `data/instance-specs.json` → `burstable_ec2_specs` keys. If the source instance_type prefix (e.g. `t3`) matches any key prefix in that dict, the source is burstable — no floor, any GCP family is valid. Pick cheapest.

**Sustained source (everything else):** load the burstable GCP family list from `data/gcp-model-config.json` → `gp_family_tier` (entries where value = `"burstable"`). Build an exclusion filter dynamically:
```python
burstable_families = [k for k,v in gp_family_tier.items() if v == "burstable"]
# e.g. ["E2", "T2A Arm"] — driven by config, not hardcoded
exclusion_clauses = " AND ".join(f"c.description NOT ILIKE '{f} %'" for f in burstable_families)
```
Never hardcode family names like "E2" or "T2A" directly in a query — read them from config.

**ARM source (instance_arch = 'arm64'):** load ARM families from `data/gcp-model-config.json` → `gp_family_arch` (entries where value = `"arm"`). Prioritize candidates whose description starts with any of those family names. Fall back to x86 only if no ARM SKU is available in the region; record the architecture change in `projection_note`.

**Memory-optimized source (RAM/vCPU ratio ≥ 8):** exclude families with ratio < source ratio. For each candidate, the actual RAM ratio is not in the SKU — use the ratio from the cost: `ram_rate / core_rate` correlates loosely. Better: filter by description keyword (`highmem` signals ≥ 8 GB/vCPU; `standard` signals 4 GB/vCPU). Exclude `standard` families when source ratio ≥ 8.

### Step 3 — availability check (top 5 candidates)

For the top 5 candidates by `total_hourly_usd`, verify each is actually available:

```bash
bash scripts/find-sku.sh --service "Compute Engine" --region "{gcp_region}" --sku-id "{core_sku_id}"
```

Pick the first candidate where both `core_sku_id` and `ram_sku_id` return results. Record which candidates were checked and skipped in `mapping-notes.md`.

### Step 4 — CUD pairing

Once you pick a family, also look up the `Commit1Yr` and `Commit3Yr` SKUs for the same family:

```sql
SELECT gcp_sku_id, description, rate_usd, pricing_type
FROM gcp_sku_rates
WHERE gcp_service = 'Compute Engine'
  AND region      = '{gcp_region}'
  AND resource_group IN ('CPU', 'RAM')
  AND pricing_type IN ('Commit1Yr', 'Commit3Yr')
  AND description ILIKE '%{family_prefix}%'
ORDER BY pricing_type, resource_group;
```

The same family prefix (e.g. `N2D AMD`) will have both OD and CUD SKUs — include the CUD SKU IDs in the mapping so Phase 4 can compute the CUD savings columns.

---

## Spot / Preemptible routing

If `pricing_model = 'Spot'`:
```sql
SELECT gcp_sku_id, description, rate_usd
FROM gcp_sku_rates
WHERE gcp_service  = 'Compute Engine'
  AND region       = '{gcp_region}'
  AND pricing_type = 'OnDemand'  -- GCP Spot is OD-priced; no separate Spot SKU type
  AND resource_group = 'CPU'
  AND description ILIKE '%preemptible%'
ORDER BY rate_usd ASC;
```
Add to `projection_note`: `"Spot/Preemptible — GCP Preemptible VMs can't be combined with CUDs"`.

---

## Size token → vCPU (fallback only — prefer instance_vcpus from catalog)

Load from `data/instance-specs.json` → `ec2_size_vcpu_fallback` if that key exists. Otherwise derive from `burstable_ec2_specs` entries or use cross-instance-family knowledge — flag as `Open:` in mapping-notes.md so Phase 3 can verify. Never embed a static size table in this prompt; the config is the single source of truth.

Load RAM-per-vCPU ratios from `data/instance-specs.json` → `arm_ram_ratio` for ARM families, and cross-check against `burstable_ec2_specs` `[vcpu, ram_gb]` entries for burstable types.

---

## Confidence levels

Load all confidence values from `data/review-config.json`. Do not hardcode numbers here — use the keys:

| Scenario | config key |
|---|---|
| Family found, both core+ram SKUs verified | `confidence_compute_verified` |
| ARM → fell back to x86 (architecture change) | `confidence_compute_arm_fallback_x86` |
| vCPU/RAM derived from size token (not catalog) | `confidence_compute_vcpu_from_size_token` |
| Only 1 candidate in region after filtering | `confidence_compute_single_candidate` |
