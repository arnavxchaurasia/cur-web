#!/usr/bin/env python3
"""
build_catalog_index.py — Pure-Python, cross-platform replacement for
build-catalog-index.sh.  Builds data/catalog.duckdb from the bundled
data/skus/*.json.gz files without any bash or external tool dependency.

Usage:
    python scripts/build_catalog_index.py
"""
import glob, gzip, json, os, shutil, sys, tempfile

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import duckdb

SKILL_DIR = os.environ.get("SKILL_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR   = os.path.join(SKILL_DIR, "data")
FINAL_PATH = os.path.join(DATA_DIR, "catalog.duckdb")


def build():
    # Build into a temp directory to avoid AV / server locking the WAL mid-write.
    tmp_dir    = tempfile.mkdtemp()
    index_path = os.path.join(tmp_dir, "catalog.duckdb")
    try:
        con = duckdb.connect(index_path)
        con.execute("""
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
        """)
        con.execute("""
            CREATE TABLE tiered_rates (
                sku_id     VARCHAR,
                tier_start DOUBLE,
                rate_usd   DOUBLE
            )
        """)

        sku_batch, rate_batch = [], []
        BATCH = 5000

        def flush():
            if sku_batch:
                con.executemany("INSERT INTO skus VALUES (?,?,?,?,?,?,?,?)", sku_batch)
                sku_batch.clear()
            if rate_batch:
                con.executemany("INSERT INTO tiered_rates VALUES (?,?,?)", rate_batch)
                rate_batch.clear()

        total = 0
        gz_files = sorted(glob.glob(os.path.join(DATA_DIR, "skus", "*.json.gz")))
        print(f"  build_catalog: loading {len(gz_files)} SKU files ...")
        for gz_path in gz_files:
            with gzip.open(gz_path) as f:
                skus = json.load(f)
            for sku in skus:
                sid  = sku.get("skuId", "")
                cat  = sku.get("category", {})
                expr = (sku.get("pricingInfo") or [{}])[0].get("pricingExpression", {})
                sku_batch.append((
                    sid,
                    cat.get("serviceDisplayName", ""),
                    cat.get("resourceFamily", ""),
                    cat.get("resourceGroup", ""),
                    cat.get("usageType", ""),
                    sku.get("description", ""),
                    expr.get("usageUnit", ""),
                    sku.get("serviceRegions", []),
                ))
                for tier in expr.get("tieredRates", []):
                    price = tier.get("unitPrice", {})
                    rate  = int(price.get("units") or 0) + (price.get("nanos") or 0) / 1e9
                    rate_batch.append((sid, tier.get("startUsageAmount", 0), rate))
                total += 1
                if len(sku_batch) >= BATCH:
                    flush()
        flush()

        con.execute("CREATE INDEX idx_skus_svc  ON skus(service_name)")
        con.execute("CREATE INDEX idx_skus_rg   ON skus(resource_group)")
        con.execute("CREATE INDEX idx_skus_ut   ON skus(usage_type)")
        con.execute("CREATE INDEX idx_rates_sku ON tiered_rates(sku_id)")
        try:
            con.close()
        except Exception as e:
            # Windows: autocheckpoint may fail because AV holds the WAL open briefly.
            # The COMMIT already succeeded and data is durable in the WAL; we proceed
            # with the move — DuckDB will replay the WAL on next open.
            print(f"  build_catalog: checkpoint warning (continuing): {e}", file=sys.stderr)

        # Move finished DB (+ WAL if present) to the skill data dir.
        # Moving the WAL alongside the main file lets DuckDB recover on next open.
        for suffix in ("", ".wal"):
            src = index_path + suffix
            dst = FINAL_PATH + suffix
            if os.path.exists(dst):
                try:
                    os.remove(dst)
                except Exception:
                    pass
            if os.path.exists(src):
                shutil.move(src, dst)

        print(f"  build_catalog: done — {total} SKUs written to {FINAL_PATH}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    build()
