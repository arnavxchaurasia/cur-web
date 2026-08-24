# Phase 2 — Data transfer / egress mapping

Handles `mechanic_group = 'data_transfer'` (unclassified rows after `classify_transfer.py`). Read `_shared.md` first.

---

## Context extraction

```
aws_li_key         — row identifier
gcp_region         — source GCP region (where traffic originates)
product            — "AWS Data Transfer", "Amazon EC2", "Amazon S3", etc.
usage_type         — direction signal: "DataTransfer-Out-Bytes", "DataTransfer-Regional-Bytes", etc.
operation          — authoritative: "regional data transfer", "inter-region", "internet", etc.
total_usage        — GB transferred (needed to compute gcp_projected_cost = total_usage × rate_usd for back-check)
unit               — 'GB'
aws_amortized_cost — for back-check
```

---

## Direction → GCP SKU routing

Load resource_group constants from `data/storage-sku-keywords.json` → `transfer_resource_groups`. Do not hardcode strings — reference config keys:

| Signal in operation / usage_type | GCP SKU family | config key |
|---|---|---|
| `"regional data transfer"` / `"inter-AZ"` / `DataTransfer-Regional` | Compute Engine inter-zone egress | `transfer_resource_groups["interzone_egress"]` |
| `"inter-region"` / cross-region within continent | Compute Engine inter-region egress | `transfer_resource_groups["interregion_egress"]` |
| `"internet"` / `DataTransfer-Out-Bytes` | Compute Engine internet egress | `transfer_resource_groups["internet_egress"]` |
| `"CloudFront"` → internet | Cloud CDN egress | CDN SKU |
| `"Direct Connect"` / `"VPN"` | Cloud Interconnect / Cloud VPN | `passthrough` if port cost not in bill |
| `"NAT Gateway"` processed bytes | Cloud NAT processed bytes | Networking |
| Same-AZ (free on AWS) | `strategy='ignore'` | — |
| S3 cross-region replication | passthrough | No direct SKU |

---

## Dynamic candidate sweep

### Internet egress

```sql
SELECT gcp_sku_id, description, resource_group, rate_usd
FROM gcp_sku_rates
WHERE gcp_service  = 'Compute Engine'
  AND region       = '{gcp_region}'
  AND pricing_type = 'OnDemand'
  AND resource_group = 'InternetEgress'
  AND rate_usd     > 0
ORDER BY rate_usd ASC;
```

GCP internet egress has tiered pricing — Phase 4's blended-rate computation handles this. Just pick the correct regional SKU. The description must match the source region (e.g. "Premium Network Egress from Americas to …") — never pick a Singapore SKU for traffic from us-east1.

### Inter-zone / inter-region egress

```sql
SELECT gcp_sku_id, description, resource_group, rate_usd
FROM gcp_sku_rates
WHERE gcp_service  IN ('Compute Engine', 'Networking')
  AND region       = '{gcp_region}'
  AND pricing_type = 'OnDemand'
  AND resource_group IN ({interregion}, {interzone})  -- load from transfer_resource_groups["interregion_egress"], transfer_resource_groups["interzone_egress"]
  AND rate_usd     > 0
ORDER BY rate_usd ASC;
```

### Availability check

```bash
bash scripts/find-sku.sh --service "Compute Engine" --region "{gcp_region}" --sku-id "{candidate_sku_id}"
```

Verify the SKU description references the correct source region before picking.

---

## unit_multiplier

`unit_multiplier = 1.0` for all egress rows — both AWS and GCP bill per GB.

---

## Confidence levels

Load all confidence values from `data/review-config.json`. Do not hardcode numbers here — use the keys:

| Scenario | config key |
|---|---|
| Internet egress, region verified | `confidence_transfer_internet_egress` |
| Inter-zone egress | `confidence_transfer_interzone` |
| NAT Gateway → Cloud NAT | `confidence_transfer_nat` |
| CloudFront → Cloud CDN | `confidence_transfer_cloudfront_cdn` |
| Direct Connect / VPN (passthrough) | `confidence_passthrough` |
