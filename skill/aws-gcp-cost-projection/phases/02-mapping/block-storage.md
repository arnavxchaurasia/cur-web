# Phase 2 — Block storage mapping (EBS → Persistent Disk / Hyperdisk)

Handles `mechanic_group = 'block_storage'`. Read `_shared.md` first.

---

## Context extraction

```
aws_li_key         — row identifier
gcp_region         — target GCP region
volume_type        — gp2 / gp3 / io1 / io2 / st1 / sc1 / magnetic
                     Aurora storage / EFS Standard / EFS IA
operation          — raw operation string; check for 'Block Express' to route io2 to Extreme PD
iops_provisioned   — for io1/io2 rows (may be NULL for gp rows)
throughput_mibps   — for gp3 rows (may be NULL)
total_usage        — GB-Mo (storage capacity charge) or IOPS count or MiB/s
unit               — 'GB-Mo', 'IOPS-Mo', 'MiB/s-Mo'
aws_amortized_cost — for back-check
```

Each EBS volume can appear as multiple rows in the bill: one for capacity (GB-Mo), one for provisioned IOPS (io1/io2/gp3), one for provisioned throughput (gp3). Map each row separately by its `unit`.

---

## Dynamic candidate sweep

### Step 1 — query all storage SKUs in the target region

```sql
SELECT gcp_sku_id, description, resource_group, rate_usd
FROM gcp_sku_rates
WHERE gcp_service  = 'Compute Engine'
  AND region       = '{gcp_region}'
  AND pricing_type = 'OnDemand'
  AND resource_group IN ('Storage', 'SSD', 'LocalSSD')
  AND rate_usd     > 0
ORDER BY rate_usd ASC;
```

Also sweep Hyperdisk SKUs:
```sql
SELECT gcp_sku_id, description, resource_group, rate_usd
FROM gcp_sku_rates
WHERE gcp_service  = 'Compute Engine'
  AND region       = '{gcp_region}'
  AND pricing_type = 'OnDemand'
  AND description  ILIKE '%Hyperdisk%'
  AND rate_usd     > 0
ORDER BY rate_usd ASC;
```

### Step 2 — apply volume_type performance floor

Load all description keywords and resource_group values from `data/storage-sku-keywords.json` → `ebs_volume_type_keywords`. Do not hardcode ILIKE strings — read them from the config key for the specific `volume_type`. Example pattern:

```python
kw = storage_sku_keywords["ebs_volume_type_keywords"][volume_type]["description_keyword"]
# Then use in query: AND description ILIKE '%{kw}%'
```

Key rules per volume type (use config keywords, not the strings below — they are examples only):

**gp2**: Use `ebs_volume_type_keywords["gp2"]["description_keyword"]`. No companion IOPS or throughput fee.

**gp3**: Three separate rows possible — capacity, IOPS, throughput. Use the `gp3_capacity`, `gp3_iops`, and `gp3_throughput` keys. Check `companion_iops` and `companion_throughput` flags from the config to decide which companion rows apply. All three must be from the same Hyperdisk Balanced family.

**io1**: Use `ebs_volume_type_keywords["io1"]["description_keyword"]`. If `iops_provisioned` never exceeds 160,000 or 2,400 MiB/s, this is the correct target.

**io2**: If `operation` or description contains "Block Express", use `ebs_volume_type_keywords["io2_block_express"]["description_keyword"]`. Otherwise use `"io2"` key.

**st1 / sc1 / magnetic**: Use the respective key's `description_keyword`. Never use "pd-standard" — that never matches a real SKU.

**aurora_storage**: Use `"aurora_storage"` or `"aurora_storage_alt"` key depending on which matches the bill description.

**EFS Standard**: Use `"efs_standard"` key. Note: a cheaper Zonal tier exists but has a different pricing shape — incompatible with GB-Mo-only EFS data.

**EFS Standard-IA / One Zone-IA**: Use `"efs_ia"` key.

### Step 3 — availability check

```bash
bash scripts/find-sku.sh --service "Compute Engine" --region "{gcp_region}" --sku-id "{candidate_sku_id}"
```

Iterate candidates cheapest-first. Pick first available.

---

## unit_multiplier

| AWS unit | GCP unit | multiplier |
|---|---|---|
| GB-Mo | GiBy.mo | 1.0 (treat GB ≈ GiB for storage) |
| IOPS count | IOPS | 1.0 |
| MiB/s | MiB/s | 1.0 |

---

## Confidence levels

Load all confidence values from `data/review-config.json`. Do not hardcode numbers here — use the keys:

| Scenario | config key |
|---|---|
| Standard volume type, SKU verified | `confidence_block_storage_verified` |
| io2 Block Express ceiling check passed | `confidence_block_storage_io2_block_express` |
| EFS → Filestore (shape difference noted) | `confidence_block_storage_efs_filestore` |
| Aurora storage (correct naming verified) | `confidence_block_storage_aurora` |
