# Phase 2 — Managed DB mapping (RDS / Aurora / ElastiCache / MemoryDB → Cloud SQL / AlloyDB / Memorystore)

Handles `mechanic_group = 'managed_db'`. Read `_shared.md` first.

---

## Context extraction

```
aws_li_key         — row identifier
gcp_region         — target GCP region
product            — "Amazon RDS", "Amazon Aurora", "Amazon ElastiCache", "Amazon MemoryDB"
engine             — mysql / postgres / aurora-mysql / aurora-postgresql / mariadb /
                     oracle / sqlserver / redis / valkey / memcached
instance_type      — e.g. db.r6g.4xlarge, cache.r6g.large
instance_vcpus     — from aws_li_catalog
instance_ram_gb    — from aws_li_catalog
instance_arch      — x86_64 or arm64
deployment_option  — 'Single-AZ' or 'Multi-AZ' (HA signal)
pricing_model      — OnDemand / Committed (RI-applied)
license_model      — 'License Included' or 'Bring Your Own License' / 'Customer-provided'
total_usage        — quantity: hours for compute rows, GB-Mo for storage, IOPS count for IOPS rows
unit               — 'Hrs' (compute), 'GB-Mo' (storage), 'IOPS' (provisioned)
aws_amortized_cost — for back-check
```

Each DB instance appears as multiple rows: compute hours (Hrs), storage (GB-Mo), backup (GB-Mo), IOPS (for provisioned). Map each row by its `unit`.

---

## GCP service routing by engine

Load resource_group constants from `data/storage-sku-keywords.json` → `db_resource_groups`. Engine→service routing stays here as business logic (engines are AWS-defined stable identifiers, not GCP catalog values):

| AWS engine | GCP service | resource_group config keys |
|---|---|---|
| mysql / mariadb | Cloud SQL | `db_resource_groups["cloud_sql_cpu"]` / `["cloud_sql_ram"]` |
| postgres | Cloud SQL (standard) or AlloyDB (high perf/HA) | same Cloud SQL keys or `["alloydb_cpu"]` / `["alloydb_ram"]` |
| aurora-postgresql | AlloyDB (preferred — closest performance tier) | `["alloydb_cpu"]` / `["alloydb_ram"]` |
| aurora-mysql | Cloud SQL for MySQL | `["cloud_sql_cpu"]` / `["cloud_sql_ram"]` |
| oracle | Cloud SQL for SQL Server (closest) or passthrough | document in projection_note |
| sqlserver | Cloud SQL for SQL Server | `["cloud_sql_cpu"]` / `["cloud_sql_ram"]` |
| redis / valkey | Cloud Memorystore for Redis | `["memorystore_redis"]` |
| memcached | Cloud Memorystore for Memcached | `["memorystore_memcached"]` |

---

## Dynamic candidate sweep — compute rows (unit = 'Hrs')

### Step 1 — get all SKUs for the target DB service in the region

```sql
SELECT gcp_sku_id, description, resource_group, rate_usd, pricing_type
FROM gcp_sku_rates
WHERE gcp_service IN ('Cloud SQL', 'AlloyDB', 'Cloud Memorystore for Redis',
                      'Cloud Memorystore for Memcached')
  AND region       = '{gcp_region}'
  AND pricing_type = 'OnDemand'
  AND rate_usd     > 0
ORDER BY gcp_service, resource_group, rate_usd ASC;
```

Then filter to the specific service based on engine routing above.

### Step 2 — HA tier filter

Load HA tier keywords from `data/storage-sku-keywords.json` → `db_ha_tier_keywords`. Do not hardcode ILIKE strings — read them from config:

**If `deployment_option = 'Multi-AZ'` or description/operation contains 'Multi-AZ' / 'HA':**
- Cloud SQL: filter `description ILIKE '%{db_ha_tier_keywords["cloud_sql_ha"]}%'`
- AlloyDB: Regional tier
- Memorystore: `description ILIKE '%{db_ha_tier_keywords["memorystore_ha"]}%'`

**Otherwise (Single-AZ / no HA signal):**
- Cloud SQL: `description NOT ILIKE '%{db_ha_tier_keywords["cloud_sql_ha"]}%'`
- Memorystore: `description ILIKE '%{db_ha_tier_keywords["memorystore_zonal"]}%'`

### Step 3 — ARM routing

If `instance_arch = 'arm64'` (Graviton-based RDS instances like db.r6g, db.m6g):
- Cloud SQL Enterprise Plus is the only tier with C4A (ARM) support
- Force `description ILIKE '%{db_ha_tier_keywords["cloud_sql_arm"]}%'`; add to `projection_note`: "C4A ARM compatibility requires Enterprise Plus"

### Step 4 — pair core + ram SKUs

Same pairing pattern as compute.md:
```sql
-- After filtering to the right service + HA tier:
SELECT
  c.gcp_sku_id, c.description, c.rate_usd,
  r.gcp_sku_id, r.description, r.rate_usd,
  (c.rate_usd * {vcpu} + r.rate_usd * {ram_gb}) AS total_hourly_usd
FROM gcp_sku_rates c
JOIN gcp_sku_rates r
  ON c.gcp_service = r.gcp_service AND c.region = r.region
  AND c.resource_group IN ({cpu_rg}, {alloydb_cpu_rg})  -- db_resource_groups["cloud_sql_cpu"], db_resource_groups["alloydb_cpu"]
  AND r.resource_group IN ({ram_rg}, {alloydb_ram_rg})  -- db_resource_groups["cloud_sql_ram"], db_resource_groups["alloydb_ram"]
  -- pair within same edition (Zonal/Regional, Enterprise/Enterprise Plus)
  AND regexp_replace(c.description, '\s*(CPU|Core).*', '') =
      regexp_replace(r.description, '\s*(RAM|Memory).*', '')
WHERE c.region = '{gcp_region}'
  AND c.pricing_type = 'OnDemand'
  AND c.rate_usd > 0 AND r.rate_usd > 0
ORDER BY total_hourly_usd ASC;
```

### Step 5 — availability check

```bash
bash scripts/find-sku.sh --service "Cloud SQL" --region "{gcp_region}" --sku-id "{candidate_sku_id}"
```

Pick first available pair.

### Step 6 — CUD pairing (for committed/RI rows)

```sql
SELECT gcp_sku_id, description, rate_usd, pricing_type
FROM gcp_sku_rates
WHERE gcp_service = '{db_service}'
  AND region      = '{gcp_region}'
  AND pricing_type IN ('Commit1Yr', 'Commit3Yr')
  AND description ILIKE '%{family_from_od_pick}%'
ORDER BY pricing_type, resource_group;
```

---

## Dynamic candidate sweep — storage rows (unit = 'GB-Mo')

```sql
SELECT gcp_sku_id, description, resource_group, rate_usd
FROM gcp_sku_rates
WHERE gcp_service IN ('Cloud SQL', 'AlloyDB')
  AND region       = '{gcp_region}'
  AND pricing_type = 'OnDemand'
  AND resource_group ILIKE '%Storage%'
  AND rate_usd     > 0
ORDER BY rate_usd ASC;
```

Filter to storage tier matching the source: SSD for mysql/postgres/aurora, HDD/Standard only if explicitly the cheapest tier in the source.

`unit_multiplier = 1.0` (GB-Mo → GB-Mo).

---

## Dynamic candidate sweep — IOPS rows (unit = 'IOPS')

```sql
SELECT gcp_sku_id, description, rate_usd
FROM gcp_sku_rates
WHERE gcp_service  = 'Cloud SQL'
  AND region       = '{gcp_region}'
  AND pricing_type = 'OnDemand'
  AND description  ILIKE '%IOPS%'
  AND rate_usd     > 0
ORDER BY rate_usd ASC;
```

`unit_multiplier = 1.0` (IOPS count).

---

## Memorystore (ElastiCache Redis / Valkey / Memcached)

Source: `cache.<family>.<size>` instance type. Context: `instance_ram_gb` from catalog.

**Candidate sweep:**
```sql
SELECT gcp_sku_id, description, resource_group, rate_usd
FROM gcp_sku_rates
WHERE gcp_service IN ('Cloud Memorystore for Redis', 'Cloud Memorystore for Memcached')
  AND region       = '{gcp_region}'
  AND pricing_type = 'OnDemand'
  AND rate_usd     > 0
ORDER BY rate_usd ASC;
```

Filter by:
- Redis cluster mode (ElastiCache Redis cluster enabled) → `description ILIKE '%Cluster%'`
- HA (Multi-AZ) → `description ILIKE '%Standard%'` (not Basic)
- Capacity: find the tier where `max_gib ≥ instance_ram_gb` — check `data/gcp-model-config.json` → `memorystore_tier_max_gib` for tier ceilings

`unit_multiplier = 1.0` for RAM-based billing (GB-Mo).

---

## Confidence levels

Load all confidence values from `data/review-config.json`. Do not hardcode numbers here — use the keys:

| Scenario | config key |
|---|---|
| Engine + HA tier + SKU verified | `confidence_db_verified` |
| Aurora-PG → AlloyDB (close but not identical) | `confidence_db_aurora_alloydb` |
| ARM → Cloud SQL Enterprise Plus forced | `confidence_db_arm_enterprise_plus` |
| Oracle → Cloud SQL SQL Server (cross-engine) | `confidence_db_oracle_cross_engine` |
| ElastiCache Memcached → Memorystore Memcached | `confidence_db_memcached` |
| Redis cluster mode → Memorystore Redis Cluster | `confidence_db_redis_cluster` |
