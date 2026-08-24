# Phase 2 — Misc mapping

Handles `mechanic_group = 'misc'` and any group with < 5 rows (merged into misc before dispatch). Read `_shared.md` first.

---

## Context extraction

```
aws_li_key         — row identifier
gcp_region         — target GCP region
product            — AWS product name
usage_type         — from aws_li_catalog
operation          — authoritative detail field
total_usage        — quantity in billing unit (needed to estimate gcp_projected_cost for back-check)
unit               — billing unit string (e.g. 'Requests', 'GB-Mo', 'Hrs')
misc_annotation    — injected by classify_mechanics.py — read this first
  .why             — why no mechanic rule matched
  .service_hint    — AWS service name (if identified)
  .available_from_cur     — fields that are present in the row
  .not_available_cur_only — fields that would help but aren't in the bill
  .mapping_guidance       — what to do
aws_amortized_cost — for back-check
```

---

## Protocol per row

**1. `service_hint` is set** → you know the AWS service. Look it up in `data/services.json`. Follow `mapping_guidance`. For fields in `not_available_cur_only`, pick conservative defaults and record each assumption under `Assumed:` in mapping-notes.md.

**2. `service_hint` is null, `aws_amortized_cost < $10`** → `strategy='passthrough'`, `mapping_confidence=0.3`, note: "low-spend unrecognized service".

**3. `service_hint` is null, `aws_amortized_cost ≥ $10`** → search `data/services.json` by product name. If found, map. If not found, `strategy='outlier_triage'` — add to mapping-notes.md with the `why` text so Phase 5 can handle it.

---

## Dynamic candidate sweep (per service_hint)

For each identified AWS service, query `gcp_sku_rates` for all candidates in the mapped GCP service:

```sql
SELECT gcp_sku_id, description, resource_group, rate_usd
FROM gcp_sku_rates
WHERE gcp_service  = '{mapped_gcp_service}'
  AND region       = '{gcp_region}'
  AND pricing_type = 'OnDemand'
  AND rate_usd     > 0
ORDER BY rate_usd ASC;
```

Then filter by the relevant resource_group or description keyword for the specific charge type. Verify availability and pick cheapest that matches.

### Service mappings

Load from `data/aws-to-gcp-service-map.json` → `service_map`. Do not hardcode any service name, GCP service, or resource_group here — read them at runtime:

```python
svc_map = aws_to_gcp_service_map["service_map"]
entry = svc_map.get(service_hint)
# entry has: gcp_service, resource_group_hint, strategy (and optionally note)
# strategy: "map" | "break_down" | "passthrough" | "ignore"
```

If `service_hint` is not in the map, fall back to searching `data/services.json` by product name.

Sweep all candidates in the mapped GCP service before picking — do not hardcode a single target SKU per service.

---

## Confidence levels

Load all confidence values from `data/review-config.json`. Do not hardcode numbers here — use the keys:

| Scenario | config key |
|---|---|
| service_hint set, SKU verified | `confidence_misc_service_hint_verified` |
| service_hint set, some fields assumed | `confidence_misc_service_hint_assumed` |
| No service_hint, matched via data/services.json | `confidence_misc_no_hint_matched` |
| Outlier_triage | `confidence_passthrough` |
| Passthrough (low-spend unrecognized) | `confidence_passthrough` |
