# Phase 2 — GPU / Accelerator mapping

Handles `workload_class = 'GPU'` rows within `compute_breakdown`. Read `_shared.md` and `compute.md` first.

---

## Context extraction

```
aws_li_key         — row identifier
gcp_region         — target GCP region
instance_type      — e.g. p4d.24xlarge, g4dn.12xlarge, trn1.2xlarge
instance_vcpus     — from aws_li_catalog
instance_ram_gb    — from aws_li_catalog
instance_arch      — 'x86_64' or 'arm64' (critical: g5g is arm64 → must route to ARM+GPU family)
pricing_model      — 'OnDemand', 'Spot', 'Committed'
gpu_count          — number of GPUs per instance (load from gpu_count_per_instance config; fallback to 1)
gpu_model          — derive at runtime: extract family prefix from instance_type, look up gpu_aws_family_to_model
usage_quantity     — hours billed this period (needed for back-check projected cost)
aws_amortized_cost — for back-check
```

Always emit three components per GPU row: `core`, `ram`, `accelerator`.

---

## AWS GPU family → GCP GPU model

This is a **performance floor** lookup — the GCP GPU must be equal or better generation than the AWS GPU. Load ALL mappings from config — do not hardcode any family names here:

```python
# Step 1: instance_type family prefix → GPU model
gpu_model = gcp_model_config["gpu_aws_family_to_model"][family_prefix]
# e.g. "g4dn" → "T4", "p5" → "H100", "f1" → "FPGA"

# Step 2: GPU model → paired GCP compute family
gcp_family = gcp_model_config["gpu_model_paired_family"][gpu_model]
# e.g. "T4" → "N1 Predefined", "H100" → "A3", "FPGA" → "passthrough"

# Step 3: GPU model → SKU description keyword for the catalog sweep
sku_keyword = gcp_model_config["gpu_sku_description_keyword"][gpu_model]
# e.g. "T4" → "Tesla T4", "A100" → "NVIDIA Tesla A100"

# Step 4: instance_type → GPU count
gpu_count = gcp_model_config["gpu_count_per_instance"][instance_type]
# Fall back to 1 if not found; flag Open: in mapping-notes.md
```

If `gcp_family == "passthrough"` (FPGA), set `strategy='passthrough'`, `mapping_confidence=confidence_passthrough`.

---

## Dynamic candidate sweep

### Step 1 — get all GPU SKUs for the target GCP family in the region

```sql
SELECT gcp_sku_id, description, resource_group, rate_usd
FROM gcp_sku_rates
WHERE gcp_service  = 'Compute Engine'
  AND region       = '{gcp_region}'
  AND pricing_type = 'OnDemand'
  AND resource_group = 'GPU'
  AND description  ILIKE '%{gpu_model_keyword}%'
  AND rate_usd     > 0
ORDER BY rate_usd ASC;
```

Load the correct keyword from `data/gcp-model-config.json` → `gpu_sku_description_keyword[gpu_model]`. Never hardcode keyword strings.

### Step 2 — sweep core + ram for the GPU family

```sql
-- Same paired query as compute.md, filtered to the GPU instance family
SELECT c.gcp_sku_id, c.description, c.rate_usd,
       r.gcp_sku_id, r.description, r.rate_usd,
       (c.rate_usd * {vcpu} + r.rate_usd * {ram_gb}) AS compute_hourly_usd
FROM gcp_sku_rates c
JOIN gcp_sku_rates r
  ON c.region = r.region AND c.pricing_type = r.pricing_type
  AND c.resource_group = 'CPU' AND r.resource_group = 'RAM'
  AND c.description ILIKE '%{gcp_gpu_family}%'
  AND r.description ILIKE '%{gcp_gpu_family}%'
WHERE c.region = '{gcp_region}' AND c.pricing_type = 'OnDemand'
ORDER BY compute_hourly_usd ASC;
```

### Step 3 — availability check (critical for GPU — not all regions have all GPUs)

```bash
bash scripts/find-sku.sh --service "Compute Engine" --region "{gcp_region}" --sku-id "{gpu_sku_id}"
bash scripts/find-sku.sh --service "Compute Engine" --region "{gcp_region}" --sku-id "{core_sku_id}"
```

If the GPU is unavailable in the target region, check neighboring regions and document in `projection_note`. GPU availability is sparse — always verify before picking.

---

## unit_multiplier

| component | unit_multiplier |
|---|---|
| core | `instance_vcpus` |
| ram  | `instance_ram_gb` |
| accelerator | `gpu_count` (number of GPUs per instance) |

GPU SKU is priced per GPU-hour. Find the count from the instance type spec or `aws_li_catalog`.

---

## Confidence levels

Load all confidence values from `data/review-config.json`. Do not hardcode numbers here — use the keys:

| Scenario | config key |
|---|---|
| Exact GPU model available in region | `confidence_gpu_verified` |
| GPU generation upgrade (e.g. V100 → A100) | `confidence_gpu_generation_upgrade` |
| Legacy GPU → modern (e.g. K80 → A100) | `confidence_gpu_legacy_to_modern` |
| GPU unavailable in exact region, nearest noted | `confidence_gpu_region_fallback` |
| FPGA (passthrough) | `confidence_passthrough` |
