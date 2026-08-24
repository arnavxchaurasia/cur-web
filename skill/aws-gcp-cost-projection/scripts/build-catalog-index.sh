#!/usr/bin/env bash
# Build data/catalog.duckdb from the bundled SKU gzip files.
# Run once after refresh-catalog.sh, or any time the skus/ directory changes.
# Requires: python3 with duckdb package, gzip on PATH.
#
# Output: data/catalog.duckdb — two tables:
#   skus         — one row per SKU (sku_id, service_name, resource_group,
#                  usage_type, description, usage_unit, service_regions[])
#   tiered_rates — one row per pricing tier per SKU (sku_id, tier_start, rate_usd)
#
# The tiered_rates table is what enables blended-rate computation for
# Cloud Storage, BigQuery, and internet egress (see phases/04-rate-fill.md).
# find-sku.sh queries this file instead of scanning gzip files on every call.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$SKILL_DIR/data"
INDEX="$DATA_DIR/catalog.duckdb"

python3 - "$DATA_DIR" "$INDEX" <<'PYEOF'
import sys, json, gzip, glob, os
import duckdb

data_dir, index_path = sys.argv[1], sys.argv[2]

# Remove stale index so we start clean
if os.path.exists(index_path):
    os.remove(index_path)

con = duckdb.connect(index_path)
con.execute('''
    CREATE TABLE skus (
        sku_id          VARCHAR,
        service_name    VARCHAR,
        resource_family VARCHAR,
        resource_group  VARCHAR,
        usage_type      VARCHAR,
        description     VARCHAR,
        usage_unit      VARCHAR,
        service_regions VARCHAR[]
    )
''')
con.execute('''
    CREATE TABLE tiered_rates (
        sku_id      VARCHAR,
        tier_start  DOUBLE,
        rate_usd    DOUBLE
    )
''')

sku_batch, rate_batch = [], []
BATCH = 5000

def flush():
    if sku_batch:
        con.executemany('INSERT INTO skus VALUES (?,?,?,?,?,?,?,?)', sku_batch)
        sku_batch.clear()
    if rate_batch:
        con.executemany('INSERT INTO tiered_rates VALUES (?,?,?)', rate_batch)
        rate_batch.clear()

total_skus = 0
for gz_path in sorted(glob.glob(os.path.join(data_dir, 'skus', '*.json.gz'))):
    with gzip.open(gz_path) as f:
        skus = json.load(f)
    for sku in skus:
        sid   = sku.get('skuId', '')
        cat   = sku.get('category', {})
        expr  = (sku.get('pricingInfo') or [{}])[0].get('pricingExpression', {})
        tiers = expr.get('tieredRates', [])
        sku_batch.append((
            sid,
            cat.get('serviceDisplayName', ''),
            cat.get('resourceFamily', ''),
            cat.get('resourceGroup', ''),
            cat.get('usageType', ''),
            sku.get('description', ''),
            expr.get('usageUnit', ''),
            sku.get('serviceRegions', []),
        ))
        for tier in tiers:
            price = tier.get('unitPrice', {})
            rate  = int(price.get('units') or 0) + (price.get('nanos') or 0) / 1e9
            rate_batch.append((sid, tier.get('startUsageAmount', 0), rate))
        total_skus += 1
        if len(sku_batch) >= BATCH:
            flush()

flush()

# Indexes for fast lookup patterns used by find-sku.sh
con.execute('CREATE INDEX idx_skus_svc      ON skus(service_name)')
con.execute('CREATE INDEX idx_skus_rg       ON skus(resource_group)')
con.execute('CREATE INDEX idx_skus_ut       ON skus(usage_type)')
con.execute('CREATE INDEX idx_rates_sku     ON tiered_rates(sku_id)')

con.close()
print(f"Built {index_path}: {total_skus} SKUs")
PYEOF
echo "catalog.duckdb ready at $INDEX"
