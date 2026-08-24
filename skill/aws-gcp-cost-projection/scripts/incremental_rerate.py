#!/usr/bin/env python3
"""
incremental_rerate.py — Phase 5 post-LLM rate refresh.

Phase 4 already built the full gcp_sku_rates table. Phase 5's LLM only changes
gcp_sku_id or unit_multiplier on a handful of rows. This script:
  1. Finds SKU IDs now in aws_li_to_gcp_li that have NO entry in gcp_sku_rates.
  2. Loads and upserts rates only for those missing SKUs.
  3. Recreates the projection VIEW.

This replaces running the full apply_rates.py again in Phase 5 PostLLMScripts,
which dropped and rebuilt the entire rate table (loading all catalog gzip files)
even though only 1-5 SKUs changed.
"""

import duckdb
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from projection_view import create_projection_view
from apply_rates import (
    CATALOG_DB,
    CONTAINER_CODES,
    blended_rate,
    load_cud_pct,
    flag_license_exposure,
)

JOB_DIR  = os.getcwd()
DB_PATH  = os.path.join(JOB_DIR, "projection-audit", "projection.duckdb")


def fill_missing_skus(conn):
    """Load rates for any gcp_sku_id in aws_li_to_gcp_li not already in gcp_sku_rates.

    Queries catalog.duckdb directly (indexed) — no gzip file scanning.
    """
    missing = conn.execute("""
        SELECT DISTINCT m.gcp_sku_id, m.gcp_service
        FROM aws_li_to_gcp_li m
        WHERE m.gcp_sku_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM gcp_sku_rates r
              WHERE r.gcp_sku_id = m.gcp_sku_id
          )
    """).fetchall()

    if not missing:
        print("  incremental_rerate: no new SKUs to load")
        return 0

    if not os.path.exists(CATALOG_DB):
        print(f"  incremental_rerate: catalog.duckdb not found — skipping {len(missing)} SKU(s)")
        return 0

    print(f"  incremental_rerate: loading {len(missing)} new SKU(s)")

    sku_ids = [sku_id for sku_id, _ in missing]
    sku_to_service = {sku_id: svc for sku_id, svc in missing}

    cat = duckdb.connect(CATALOG_DB, read_only=True)
    try:
        placeholders = ",".join(["?" for _ in sku_ids])
        catalog_rows = cat.execute(f"""
            SELECT s.sku_id, s.service_name, s.description, s.resource_family,
                   s.resource_group, s.usage_type, s.usage_unit, s.service_regions,
                   t.tier_start, t.rate_usd
            FROM skus s
            JOIN tiered_rates t ON t.sku_id = s.sku_id
            WHERE s.sku_id IN ({placeholders})
              AND s.usage_type IN ('OnDemand', 'Preemptible', 'Commit1Yr', 'Commit3Yr')
            ORDER BY s.sku_id, s.usage_type, t.tier_start
        """, sku_ids).fetchall()
    finally:
        cat.close()

    sku_tiers: dict[tuple, list] = defaultdict(list)
    sku_meta: dict[tuple, tuple] = {}
    for sku_id, svc_name, desc, rf, rg, ut, unit, regions, tier_start, rate_usd in catalog_rows:
        key = (sku_id, ut)
        sku_tiers[key].append({"startUsageAmount": tier_start, "rate": rate_usd})
        if key not in sku_meta:
            sku_meta[key] = (svc_name, desc, rf, rg, unit, regions or [])

    max_usage_rows = conn.execute("""
        SELECT m.gcp_sku_id, MAX(c.total_usage)
        FROM aws_li_to_gcp_li m
        JOIN aws_li_catalog c USING (aws_li_key)
        WHERE m.gcp_sku_id IS NOT NULL
        GROUP BY m.gcp_sku_id
    """).fetchall()
    max_usage = {r[0]: (r[1] or 0.0) for r in max_usage_rows}

    found = set()
    for (sku_id, ut), tiers in sku_tiers.items():
        gcp_service = sku_to_service.get(sku_id, sku_meta[(sku_id, ut)][0])
        _, desc, rf, rg, unit, regions = sku_meta[(sku_id, ut)]

        base_rate = blended_rate(tiers, max_usage.get(sku_id, 0.0)) if len(tiers) > 1 else tiers[0]["rate"]

        expanded: set[str] = set()
        for r in regions:
            if r.lower() in CONTAINER_CODES:
                expanded.update(CONTAINER_CODES[r.lower()])
            else:
                expanded.add(r)

        for region in expanded:
            conn.execute("""
                INSERT INTO gcp_sku_rates VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT DO NOTHING
            """, (sku_id, gcp_service, desc, rf, rg, ut, region, unit, base_rate,
                  "catalog.duckdb", f"catalog.duckdb#{sku_id}"))
        found.add(sku_id)
        print(f"    loaded: {gcp_service} / {sku_id}")

    missing_from_catalog = set(sku_ids) - found
    for sku_id in missing_from_catalog:
        print(f"    skip {sku_id}: not found in catalog.duckdb")

    return len(found)


def synthesize_cud_for_new_skus(conn):
    """Synthesize Commit1Yr/3yr rows for any newly-loaded SKUs that don't have them yet."""
    cud_pct = load_cud_pct()
    _default_pct = cud_pct.get("DEFAULT", (0.75, 0.60))

    _CUD_GROUPS = [
        ("Compute Engine",                    "('CPU','RAM','GPU')"),
        ("Cloud SQL",                         None),
        ("AlloyDB",                           None),
        ("Cloud Memorystore for Memcached",   None),
        ("Cloud Memorystore for Redis",       None),
        ("Cloud Memorystore",                 None),
    ]
    for svc, rg_list in _CUD_GROUPS:
        r1, r3 = cud_pct.get(svc, _default_pct)
        rg_clause = f"AND resource_group IN {rg_list}" if rg_list else ""
        for pricing_type, mult in [("Commit1Yr", r1), ("Commit3Yr", r3)]:
            conn.execute(f"""
                INSERT INTO gcp_sku_rates
                SELECT gcp_sku_id, gcp_service, gcp_sku_name, resource_family,
                       resource_group, '{pricing_type}', region, unit,
                       rate_usd * {mult}, 'doc-percentage', audit_url
                FROM gcp_sku_rates
                WHERE gcp_service = '{svc}' AND pricing_type = 'OnDemand'
                  {rg_clause}
                  AND NOT EXISTS (
                      SELECT 1 FROM gcp_sku_rates r2
                      WHERE r2.gcp_sku_id = gcp_sku_rates.gcp_sku_id
                        AND r2.pricing_type = '{pricing_type}'
                        AND r2.region = gcp_sku_rates.region
                  )
                ON CONFLICT DO NOTHING
            """)


def main():
    if not os.path.exists(DB_PATH):
        print("Database not found — nothing to re-rate.")
        sys.exit(0)

    conn = duckdb.connect(DB_PATH)

    loaded = fill_missing_skus(conn)
    if loaded:
        synthesize_cud_for_new_skus(conn)

    # Re-flag license exposure in case Phase 5 changed a row's service
    flag_license_exposure(conn)

    # Recreate projection VIEW so the Phase 5 gate sees fresh costs
    create_projection_view(conn)
    print(f"  incremental_rerate: done ({loaded} new SKU(s) loaded)")


if __name__ == "__main__":
    main()
