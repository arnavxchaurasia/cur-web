# Phase 2 — Object storage mapping (S3 → Cloud Storage)

Handles `mechanic_group = 'object_storage'` (rows not resolved by `apply_static_mappings.py`). Read `_shared.md` first.

---

## Context extraction

```
aws_li_key         — row identifier
gcp_region         — target GCP region
product            — "Amazon Simple Storage Service"
usage_type         — storage class signal (TimedStorage-*, Requests-*, DataTransfer-*)
operation          — authoritative detail: storage class, request type, transfer direction
total_usage        — quantity in billing unit: GB-Mo for storage rows, count for request rows (needed for back-check)
unit               — 'GB-Mo', 'Requests', 'GB'
aws_amortized_cost — for back-check
```

S3 rows appear in multiple charge types. The `operation` field identifies which:
- `"StandardStorage"` / `"IntelligentTiering"` / `"GlacierStorage"` → storage capacity (GB-Mo)
- `"PutObject"` / `"GetObject"` / `"Class A Ops"` / `"Class B Ops"` → request fees
- `"DataTransfer"` → egress (route to data-transfer.md instead)
- `"S3-SELECT-Scanned"` / `"S3-SELECT-Returned"` → S3 Select (passthrough — no GCS equivalent)

---

## S3 class → GCS class mapping (performance floor)

Load from `data/storage-sku-keywords.json` → `s3_to_gcs_class`. Do not hardcode the mapping table here — read it at runtime:

```python
s3_map = storage_sku_keywords["s3_to_gcs_class"]
entry = s3_map.get(operation_field)
# entry has: gcs_class, description_keyword, resource_group (and optionally note/strategy)
```

**Performance floor rule:** never map to a CHEAPER GCS class that has a LOWER availability SLA than the S3 class (e.g., do not map Standard → Nearline; Nearline → Archive).

---

## Dynamic candidate sweep — storage rows (unit = 'GB-Mo')

```sql
SELECT gcp_sku_id, description, resource_group, rate_usd
FROM gcp_sku_rates
WHERE gcp_service  = 'Cloud Storage'
  AND region       = '{gcp_region}'
  AND pricing_type = 'OnDemand'
  AND resource_group = 'StorageUsage'
  AND rate_usd     > 0
ORDER BY rate_usd ASC;
```

Filter by GCS class keyword from `s3_to_gcs_class[operation]["description_keyword"]`, using `description ILIKE '%{keyword}%'`.

Then filter by region type using `data/storage-sku-keywords.json` → `gcs_region_type_keyword`:
- Multi-region gcp_region (us, eu, asia): append `description ILIKE '%{gcs_region_type_keyword["multi_region"]}%'`
- Single region: no additional filter (or `NOT ILIKE '%Multi-Region%'` if needed)

`unit_multiplier = 1.0` (GB-Mo).

---

## Dynamic candidate sweep — request rows (unit = 'Requests')

```sql
SELECT gcp_sku_id, description, resource_group, rate_usd
FROM gcp_sku_rates
WHERE gcp_service  = 'Cloud Storage'
  AND region       = '{gcp_region}'
  AND pricing_type = 'OnDemand'
  AND resource_group IN ({resource_groups})  -- load from storage-sku-keywords.json → gcs_request_resource_groups values
  AND rate_usd     > 0
ORDER BY rate_usd ASC;
```

S3 PUT/POST/LIST/COPY → Class A operations. S3 GET/HEAD/SELECT → Class B.

`unit_multiplier = 1.0` (GCS also bills per operation). Back-check: `gcp_projected / aws_amortized` must be 0.33–3.0.

---

## Availability check

```bash
bash scripts/find-sku.sh --service "Cloud Storage" --region "{gcp_region}" --sku-id "{candidate_sku_id}"
```

---

## Confidence levels

Load all confidence values from `data/review-config.json`. Do not hardcode numbers here — use the keys:

| Scenario | config key |
|---|---|
| Standard → Standard, SKU verified | `confidence_object_storage_standard` |
| IA → Nearline (30-day billing noted) | `confidence_object_storage_ia_nearline` |
| Glacier → Coldline/Archive | `confidence_object_storage_glacier` |
| Intelligent-Tiering infrequent tier | `confidence_object_storage_intelligent_tiering` |
| S3 Select (passthrough) | `confidence_passthrough` |
