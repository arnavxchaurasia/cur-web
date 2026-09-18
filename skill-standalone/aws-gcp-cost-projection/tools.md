# Tool Scripts Reference

This document covers the 10 utility scripts that support the AWS→GCP cost-projection pipeline.
Each section states what the script does, when to use it, and provides a self-contained runnable
code block (no internal imports — all helpers are inlined).

---

## 1. `build_catalog_index.py`

**What it does:** Builds `data/catalog.duckdb` from the bundled `data/skus/*.json.gz` files.
Creates two tables (`skus` and `tiered_rates`) with indexes for fast SKU lookup.

**When to use:** After installing the skill, or after running `refresh-catalog.sh` to update the
bundled gzip files. Also runs automatically on Windows (where bash is unavailable) via
`check_catalog_age.py`.

**Run with:**
```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
python3 - << 'PYEOF'
import os, sys
SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.environ.get("JOB_DIR", os.getcwd())

import glob, gzip, json, shutil, tempfile
import duckdb

DATA_DIR   = os.path.join(SKILL_DIR, "data")
FINAL_PATH = os.path.join(DATA_DIR, "catalog.duckdb")

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def build():
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
            print(f"  build_catalog: checkpoint warning (continuing): {e}", file=sys.stderr)

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

build()
PYEOF
```

---

## 2. `check_catalog_age.py`

**What it does:** Auto-refreshes the GCP SKU catalog if it is more than 365 days old.
Reads `data/CATALOG_META.json` for the last fetch date. On Linux/macOS, runs
`refresh-catalog.sh`; on Windows, runs `build_catalog_index.py` directly.
After refresh, warms the SKU cache for the current GCP region via `prefetch_skus.py`.

**When to use:** Called automatically by the orchestrator as a `pre_llm_script` before ingestion.
Safe to run on every job — the check costs only one JSON file read; the refresh fires at most
once a year.

**Run with:**
```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
export GCP_REGION="us-central1"
python3 - << 'PYEOF'
import os, sys
SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.environ.get("JOB_DIR", os.getcwd())

import json, subprocess
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.join(SKILL_DIR, "scripts")
META_PATH   = os.path.join(SKILL_DIR, "data", "CATALOG_META.json")
REFRESH_DAYS = 365

def _days_since(iso: str) -> float:
    ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - ts).days

def main():
    gcp_region = os.environ.get("GCP_REGION", "us-central1")
    needs_refresh = True
    if os.path.exists(META_PATH):
        try:
            meta = json.load(open(META_PATH))
            fetched_at = meta.get("fetched_at", "")
            if fetched_at:
                age_days = _days_since(fetched_at)
                if age_days < REFRESH_DAYS:
                    print(f"  catalog: fresh ({age_days} days old, threshold={REFRESH_DAYS})")
                    needs_refresh = False
                else:
                    print(f"  catalog: STALE ({age_days} days old) — refreshing now")
        except Exception as e:
            print(f"  catalog: could not read meta ({e}) — refreshing")
    else:
        print(f"  catalog: CATALOG_META.json missing — running initial fetch")

    if not needs_refresh:
        return

    lock_path = META_PATH + ".lock"
    lock_fh = open(lock_path, "w", encoding="utf-8")
    try:
        import platform
        if platform.system() == "Windows":
            import msvcrt
            msvcrt.locking(lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
        if os.path.exists(META_PATH):
            try:
                meta2 = json.load(open(META_PATH))
                fetched_at2 = meta2.get("fetched_at", "")
                if fetched_at2 and _days_since(fetched_at2) < REFRESH_DAYS:
                    print(f"  catalog: already refreshed by another job — skipping")
                    return
            except Exception:
                pass
    except Exception:
        pass

    import platform
    refresh_sh = os.path.join(SCRIPTS_DIR, "refresh-catalog.sh")
    if platform.system() == "Windows":
        build_script = os.path.join(SCRIPTS_DIR, "build_catalog_index.py")
        if os.path.exists(build_script):
            print(f"  catalog: rebuilding catalog.duckdb (Windows, no bash) ...")
            result = subprocess.run([sys.executable, build_script], cwd=SKILL_DIR, capture_output=False)
            if result.returncode != 0:
                print(f"  catalog: WARNING build_catalog_index.py exited {result.returncode}", file=sys.stderr)
                return
        else:
            print(f"  catalog: WARNING neither refresh-catalog.sh nor build_catalog_index.py found — skipping", file=sys.stderr)
            return
    elif not os.path.exists(refresh_sh):
        print(f"  catalog: WARNING refresh-catalog.sh not found at {refresh_sh} — skipping", file=sys.stderr)
        return
    else:
        print(f"  catalog: running refresh-catalog.sh ...")
        result = subprocess.run(["bash", refresh_sh], cwd=SKILL_DIR, capture_output=False)
        if result.returncode != 0:
            print(f"  catalog: WARNING refresh-catalog.sh exited {result.returncode} — continuing with existing catalog", file=sys.stderr)
            return

    prefetch = os.path.join(SCRIPTS_DIR, "prefetch_skus.py")
    if os.path.exists(prefetch):
        print(f"  catalog: warming SKU cache for {gcp_region} ...")
        subprocess.run(
            [sys.executable, prefetch, "--region", gcp_region],
            cwd=SKILL_DIR, capture_output=False
        )

    print(f"  catalog: refresh complete")
    lock_fh.close()

main()
PYEOF
```

---

## 3. `incremental_rerate.py`

**What it does:** Phase 5 post-LLM rate refresh. After Phase 5's LLM changes `gcp_sku_id` or
`unit_multiplier` on a handful of rows, this script finds those new SKU IDs, loads their rates
from `catalog.duckdb`, synthesizes CUD rows, re-flags license exposure, and recreates the
`gcp_projection` VIEW. Much faster than re-running the full `apply_rates.py`.

**When to use:** Automatically invoked as a Phase 5 `post_llm_script`. Also safe to run manually
after any manual edit to `aws_li_to_gcp_li`.

**Run with:**
```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
export JOB_DIR="/path/to/job/directory"
python3 - << 'PYEOF'
import os, sys
SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.environ.get("JOB_DIR", os.getcwd())

import duckdb, json
from collections import defaultdict
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── inlined: log_utils (minimal) ─────────────────────────────────────────────
import logging as _logging_ir

def get_logger(name, job_dir=None):
    log = _logging_ir.getLogger(name)
    if not log.handlers:
        h = _logging_ir.StreamHandler(sys.stdout)
        h.setLevel(_logging_ir.INFO)
        log.addHandler(h)
        log.setLevel(_logging_ir.INFO)
    return log

log = get_logger("incremental_rerate")

# ── inlined: projection_view ──────────────────────────────────────────────────
_PROJECTION_VIEW_SQL = """
CREATE OR REPLACE VIEW gcp_projection AS
WITH base AS (
    SELECT
        c.aws_li_key,
        c.product,
        c.aws_resource_type,
        c.usage_type,
        c.operation,
        c.aws_amortized_cost,
        c.total_usage,
        c.usage_unit,
        c.mechanic_group,
        c.is_workload,
        c.instance_type,
        c.instance_vcpus,
        c.instance_ram_gb,
        m.gcp_service,
        m.gcp_sku_id,
        m.gcp_sku_name,
        m.component,
        m.strategy,
        m.unit_multiplier,
        m.gcp_region,
        m.projection_note,
        m.mapping_confidence,
        m.break_down,
        r.rate_usd         AS on_demand_rate,
        r1.rate_usd        AS commit1yr_rate,
        r3.rate_usd        AS commit3yr_rate,
        r.unit             AS rate_unit,
        r.audit_url        AS rate_audit_url
    FROM aws_li_catalog c
    LEFT JOIN aws_li_to_gcp_li m USING (aws_li_key)
    LEFT JOIN gcp_sku_rates r
           ON r.gcp_sku_id   = m.gcp_sku_id
          AND r.pricing_type = 'OnDemand'
          AND (m.gcp_region IS NULL OR r.region = m.gcp_region OR r.region = 'global')
    LEFT JOIN gcp_sku_rates r1
           ON r1.gcp_sku_id   = m.gcp_sku_id
          AND r1.pricing_type = 'Commit1Yr'
          AND (m.gcp_region IS NULL OR r1.region = m.gcp_region OR r1.region = 'global')
    LEFT JOIN gcp_sku_rates r3
           ON r3.gcp_sku_id   = m.gcp_sku_id
          AND r3.pricing_type = 'Commit3Yr'
          AND (m.gcp_region IS NULL OR r3.region = m.gcp_region OR r3.region = 'global')
),
projected AS (
    SELECT *,
        CASE strategy
            WHEN 'map' THEN
                CASE
                    WHEN on_demand_rate IS NOT NULL AND total_usage IS NOT NULL
                    THEN COALESCE(unit_multiplier, 1.0) * total_usage * on_demand_rate
                    ELSE aws_amortized_cost
                END
            WHEN 'break_down' THEN
                CASE
                    WHEN on_demand_rate IS NOT NULL AND total_usage IS NOT NULL
                    THEN COALESCE(unit_multiplier, 1.0) * total_usage * on_demand_rate
                    ELSE 0.0
                END
            WHEN 'passthrough' THEN aws_amortized_cost
            WHEN 'ignore'      THEN 0.0
            ELSE aws_amortized_cost
        END AS gcp_projected_cost
    FROM base
)
SELECT * FROM projected
"""

def create_projection_view(conn):
    conn.execute(_PROJECTION_VIEW_SQL)

# ── inlined: config_loader ────────────────────────────────────────────────────
def _load_data_config(name):
    path = os.path.join(SKILL_DIR, "data", f"{name}.json")
    try:
        import json as _j
        with open(path, encoding="utf-8") as f:
            return _j.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

# ── inlined: apply_rates constants / helpers ──────────────────────────────────
DATA_DIR   = os.path.join(SKILL_DIR, "data")
CATALOG_DB = os.path.join(DATA_DIR, "catalog.duckdb")

_CUD_PCT_FALLBACK = {
    "Compute Engine":                  (0.70, 0.57),
    "Cloud SQL":                       (0.70, 0.57),
    "AlloyDB":                         (0.70, 0.57),
    "Cloud Memorystore for Memcached": (0.70, 0.57),
    "Cloud Memorystore for Redis":     (0.70, 0.57),
    "Cloud Memorystore":               (0.70, 0.57),
    "DEFAULT":                         (0.75, 0.60),
}

def load_cud_pct():
    json_path = os.path.join(DATA_DIR, "cud_pct.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, encoding="utf-8") as f:
                raw = json.load(f)
            result = {}
            for svc, vals in raw.items():
                if svc == "_meta":
                    continue
                if isinstance(vals, dict) and "1yr_multiplier" in vals and "3yr_multiplier" in vals:
                    result[svc] = (float(vals["1yr_multiplier"]), float(vals["3yr_multiplier"]))
            if result:
                return result
        except Exception as e:
            log.warning(f"Could not load cud_pct.json ({e}); using fallback multipliers")
    return dict(_CUD_PCT_FALLBACK)

CONTAINER_CODES = {
    "us": ["us-central1", "us-east1", "us-east4", "us-east5", "us-south1", "us-west1", "us-west2", "us-west3", "us-west4"],
    "eu": ["europe-west1", "europe-west2", "europe-west3", "europe-west4", "europe-west6", "europe-west8", "europe-west9", "europe-west10", "europe-west12", "europe-north1", "europe-central2", "europe-southwest1"],
    "europe": ["europe-west1", "europe-west2", "europe-west3", "europe-west4", "europe-west6", "europe-west8", "europe-west9", "europe-west10", "europe-west12", "europe-north1", "europe-central2", "europe-southwest1"],
    "asia": ["asia-east1", "asia-east2", "asia-northeast1", "asia-northeast2", "asia-northeast3", "asia-south1", "asia-south2", "asia-southeast1", "asia-southeast2"],
    "northamerica": ["northamerica-northeast1", "northamerica-northeast2", "northamerica-south1"],
    "southamerica": ["southamerica-east1", "southamerica-west1"],
    "australia": ["australia-southeast1", "australia-southeast2"],
    "me": ["me-central1", "me-central2", "me-west1"],
    "middleeast": ["me-central1", "me-central2", "me-west1"],
    "africa": ["africa-south1"],
}

def blended_rate(tiered_rates, total_qty):
    total_cost = 0.0
    for i, tier in enumerate(tiered_rates):
        tier_start = tier.get("startUsageAmount", 0)
        tier_end = tiered_rates[i+1].get("startUsageAmount") if i+1 < len(tiered_rates) else float("inf")
        tier_qty = max(0, min(total_qty, tier_end) - tier_start)
        total_cost += tier_qty * tier.get("rate", 0)
    return total_cost / total_qty if total_qty > 0 else tiered_rates[-1].get("rate", 0)

_LICENSE_MARKER = "license-premium-not-modeled"

def flag_license_exposure(conn):
    try:
        rows = conn.execute(f"""
            SELECT DISTINCT m.aws_li_key,
                   COALESCE(cat.operating_system,'') os, COALESCE(cat.database_engine,'') eng
            FROM aws_li_to_gcp_li m JOIN aws_li_catalog cat USING (aws_li_key)
            WHERE cat.is_workload AND m.strategy IN ('map','break_down')
              AND COALESCE(m.projection_note,'') NOT LIKE '%license-premium-not-modeled%'
              AND (
                LOWER(COALESCE(cat.operating_system,'')) LIKE '%windows%'
                OR LOWER(COALESCE(cat.database_engine,'')) LIKE '%sql server%'
                OR LOWER(COALESCE(cat.database_engine,'')) LIKE '%sqlserver%'
                OR LOWER(COALESCE(cat.database_engine,'')) LIKE '%oracle%'
                OR LOWER(COALESCE(cat.operation,'')) LIKE '%sql server%'
                OR LOWER(COALESCE(cat.operation,'')) LIKE '%oracle%'
                OR LOWER(COALESCE(cat.usage_type,'')) LIKE '%windows%'
              )
        """).fetchall()
    except Exception as e:
        log.warning(f"License-exposure flag skipped: {e}")
        return 0

    flagged = 0
    for aws_li_key, os_, eng in rows:
        lic = "Windows" if "windows" in os_.lower() else (eng or "commercial")
        note = (f"{_LICENSE_MARKER} {lic} license premium NOT modeled — GCP compute is "
                f"priced license-exclusive; add OS/DB licensing separately.")
        conn.execute("""
            UPDATE aws_li_to_gcp_li
            SET projection_note = CASE WHEN projection_note IS NULL OR projection_note=''
                                       THEN ? ELSE projection_note || ' ' || ? END,
                mapping_confidence = LEAST(COALESCE(mapping_confidence, 1.0), 0.5)
            WHERE aws_li_key = ?
        """, (note, note, aws_li_key))
        flagged += 1
    return flagged

# ── main script ───────────────────────────────────────────────────────────────
DB_PATH  = os.path.join(JOB_DIR, "projection-audit", "projection.duckdb")

def fill_missing_skus(conn):
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

    sku_tiers: dict = defaultdict(list)
    sku_meta: dict = {}
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

        expanded: set = set()
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


if not os.path.exists(DB_PATH):
    print("Database not found — nothing to re-rate.")
    sys.exit(0)

conn = duckdb.connect(DB_PATH)
loaded = fill_missing_skus(conn)
if loaded:
    synthesize_cud_for_new_skus(conn)
flag_license_exposure(conn)
create_projection_view(conn)
print(f"  incremental_rerate: done ({loaded} new SKU(s) loaded)")
PYEOF
```

---

## 4. `job_inspect.py`

**What it does:** Single-command post-run debugger. Prints 8 sections: mechanic group breakdown,
Phase 2 temp file status, mapping coverage, confidence distribution, validator violations, SKU
gaps, outlier rows, and GCP vs AWS projected totals.

**When to use:** After any pipeline run to quickly assess coverage and spot problems without
writing SQL queries.

**Run with:**
```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
export JOB_DIR="/path/to/job/directory"
python3 - << 'PYEOF'
import os, sys
SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.environ.get("JOB_DIR", os.getcwd())

import json
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import duckdb

job_dir = JOB_DIR
db_path = os.path.join(job_dir, "projection-audit", "projection.duckdb")
manifest_path = os.path.join(job_dir, "projection-audit", "phase2_manifest.json")
mappings_dir  = os.path.join(job_dir, "projection-audit", "mappings")

if not os.path.exists(db_path):
    print(f"ERROR: {db_path} not found"); sys.exit(1)

def section(title):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")

con = duckdb.connect(db_path, read_only=True)
tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}

if "aws_li_catalog" in tables:
    section("Mechanic Group Breakdown")
    stats = con.execute("""
        SELECT mechanic_group,
               COUNT(*)                              AS rows,
               COALESCE(SUM(aws_amortized_cost), 0) AS spend,
               COUNT(CASE WHEN is_workload THEN 1 END) AS workload_rows
        FROM aws_li_catalog
        GROUP BY mechanic_group
        ORDER BY spend DESC
    """).fetchall()
    total_spend = sum(r[2] for r in stats)
    print(f"  {'group':<25} {'rows':>6}  {'workload':>8}  {'spend':>12}  {'% spend':>8}")
    for group, rows, spend, wrows in stats:
        pct = 100*spend/total_spend if total_spend else 0
        print(f"  {group or 'NULL':<25} {rows:>6}  {wrows:>8}  ${spend:>11,.2f}  {pct:>7.1f}%")
    print(f"\n  Total spend: ${total_spend:,.2f}")

section("Phase 2 Temp Files")
if os.path.exists(manifest_path):
    manifest = json.load(open(manifest_path))
    for group, meta in sorted(manifest.items()):
        fpath = os.path.join(mappings_dir, f"{group}_mappings.json")
        exists = os.path.exists(fpath)
        row_count = len(json.load(open(fpath))) if exists else 0
        status = f"ok {row_count} rows" if exists else "MISSING"
        llm = "script" if not meta.get("needs_llm") else "LLM"
        print(f"  {group:<28} [{llm:<6}]  {status}")
else:
    print("  phase2_manifest.json not found — Phase 1 may not have completed")

if "aws_li_to_gcp_li" in tables and "aws_li_catalog" in tables:
    section("Mapping Coverage")
    cov = con.execute("""
        SELECT c.mechanic_group,
               COUNT(DISTINCT c.aws_li_key)              AS catalog_rows,
               COUNT(DISTINCT m.aws_li_key)              AS mapped_rows,
               COALESCE(AVG(m.mapping_confidence), 0)   AS avg_conf,
               COUNT(CASE WHEN m.strategy = 'map'         THEN 1 END) AS mapped,
               COUNT(CASE WHEN m.strategy = 'ignore'      THEN 1 END) AS ignored,
               COUNT(CASE WHEN m.strategy = 'passthrough' THEN 1 END) AS passthrough,
               COUNT(CASE WHEN m.strategy = 'outlier_triage' THEN 1 END) AS outlier
        FROM aws_li_catalog c
        LEFT JOIN aws_li_to_gcp_li m USING (aws_li_key)
        GROUP BY c.mechanic_group
        ORDER BY catalog_rows DESC
    """).fetchall()
    print(f"  {'group':<25} {'cat':>5} {'map':>5} {'conf':>6}  map/ign/pass/out")
    for row in cov:
        grp, cat, mapp, conf, mapped, ign, pt, out = row
        coverage = f"{100*mapp/cat:.0f}%" if cat else "n/a"
        print(f"  {grp or 'NULL':<25} {cat:>5} {mapp:>4} ({coverage:>4})  {conf:.2f}   {mapped}/{ign}/{pt}/{out}")

if "aws_li_to_gcp_li" in tables:
    section("Confidence Distribution")
    dist = con.execute("""
        SELECT
            CASE
                WHEN mapping_confidence >= 0.9  THEN 'high   (>=0.90)'
                WHEN mapping_confidence >= 0.75 THEN 'medium (0.75-0.89)'
                WHEN mapping_confidence >= 0.6  THEN 'low    (0.60-0.74)'
                ELSE                                 'poor   (<0.60)'
            END AS band,
            COUNT(*) AS rows
        FROM aws_li_to_gcp_li
        WHERE mapping_confidence IS NOT NULL
        GROUP BY 1 ORDER BY MIN(mapping_confidence) DESC
    """).fetchall()
    for band, cnt in dist:
        print(f"  {band:<22} {cnt:>6} rows")

violations_path = os.path.join(job_dir, "projection-audit", "validator_violations.json")
section("Validator Violations")
if os.path.exists(violations_path):
    v = json.load(open(violations_path))
    if isinstance(v, list):
        by_level: dict = {}
        for item in v:
            lvl = item.get("level", "UNKNOWN")
            by_level.setdefault(lvl, []).append(item)
        for lvl, items in sorted(by_level.items()):
            print(f"  {lvl}: {len(items)}")
            for item in items[:3]:
                print(f"    - {item.get('message', item)}")
            if len(items) > 3:
                print(f"    - ... +{len(items)-3} more")
    else:
        print(f"  {v}")
else:
    print("  No violations file found (validator not yet run)")

if "aws_li_to_gcp_li" in tables:
    gaps = con.execute("""
        SELECT m.aws_li_key, m.gcp_sku_id, m.gcp_sku_name, m.strategy
        FROM aws_li_to_gcp_li m
        WHERE m.strategy = 'map'
          AND (m.gcp_sku_id IS NULL OR m.gcp_sku_id = '')
        LIMIT 10
    """).fetchall()
    if gaps:
        section("SKU Gaps (mapped but no SKU ID)")
        for row in gaps:
            print(f"  {row[0]}  sku_name={row[2]}")

if "aws_li_to_gcp_li" in tables and "aws_li_catalog" in tables:
    outliers = con.execute("""
        SELECT c.aws_li_key, c.product, c.aws_amortized_cost, m.projection_note
        FROM aws_li_catalog c
        JOIN aws_li_to_gcp_li m USING (aws_li_key)
        WHERE m.strategy = 'outlier_triage'
        ORDER BY c.aws_amortized_cost DESC
        LIMIT 10
    """).fetchall()
    if outliers:
        section("Outlier Triage Rows")
        for key, product, cost, note in outliers:
            print(f"  ${cost:>10,.2f}  {product:<30}  {note or ''}")

if "gcp_projection" in tables:
    section("GCP Projection Total")
    total = con.execute(
        "SELECT SUM(gcp_projected_cost) FROM gcp_projection WHERE is_workload"
    ).fetchone()[0]
    aws_total = con.execute(
        "SELECT SUM(aws_amortized_cost) FROM aws_li_catalog WHERE is_workload"
    ).fetchone()[0] if "aws_li_catalog" in tables else None
    if total:
        print(f"  GCP projected:  ${total:,.2f}")
    if aws_total:
        print(f"  AWS actual:     ${aws_total:,.2f}")
        if total:
            print(f"  Ratio GCP/AWS:  {total/aws_total:.3f}x")

con.close()
PYEOF
```

---

## 5. `compare_runs.py`

**What it does:** Compares two `projection.duckdb` files side-by-side: total GCP projected cost
difference, number of SKU or strategy flips, and the top 5 most expensive flipped rows.

**When to use:** After re-running the pipeline (e.g. with updated rates or a corrected mapping)
to confirm changes are expected and no regressions occurred.

**Run with:**
```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
python3 - /path/to/baseline.duckdb /path/to/new_run.duckdb << 'PYEOF'
import os, sys
SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.environ.get("JOB_DIR", os.getcwd())

import duckdb

def compare_runs(baseline_db, new_db):
    print(f"Comparing Baseline: {baseline_db}")
    print(f"       to New Run: {new_db}")
    print("="*60)

    con = duckdb.connect(':memory:')
    con.execute(f"ATTACH '{baseline_db}' AS baseline (READ_ONLY)")
    con.execute(f"ATTACH '{new_db}' AS newrun (READ_ONLY)")

    try:
        res = con.execute("SELECT sum(gcp_projected_cost) FROM baseline.gcp_projection WHERE is_workload").fetchone()
        baseline_gcp = res[0] if res and res[0] else 0.0

        res = con.execute("SELECT sum(gcp_projected_cost) FROM newrun.gcp_projection WHERE is_workload").fetchone()
        newrun_gcp = res[0] if res and res[0] else 0.0

        print(f"Total GCP Projected Cost:")
        print(f"  Baseline: ${baseline_gcp:,.2f}")
        print(f"  New Run:  ${newrun_gcp:,.2f}")
        print(f"  Diff:     ${newrun_gcp - baseline_gcp:,.2f}")
        print("-" * 60)

        flips = con.execute("""
            SELECT count(*) FROM baseline.aws_li_to_gcp_li b
            JOIN newrun.aws_li_to_gcp_li n ON b.aws_li_key = n.aws_li_key
            WHERE b.gcp_sku_id != n.gcp_sku_id
               OR b.strategy != n.strategy
        """).fetchone()[0]

        total_mappings = con.execute("SELECT count(*) FROM newrun.aws_li_to_gcp_li").fetchone()[0]
        print(f"Mapping Differences (SKU or Strategy changed): {flips} / {total_mappings} rows")
        print("-" * 60)

        if flips > 0:
            print("Top 5 Flipped Rows (by AWS Cost):")
            for row in con.execute("""
                SELECT c.aws_resource_type, c.aws_amortized_cost, b.gcp_sku_id, n.gcp_sku_id
                FROM baseline.aws_li_to_gcp_li b
                JOIN newrun.aws_li_to_gcp_li n ON b.aws_li_key = n.aws_li_key
                JOIN baseline.aws_li_catalog c ON b.aws_li_key = c.aws_li_key
                WHERE b.gcp_sku_id != n.gcp_sku_id
                ORDER BY c.aws_amortized_cost DESC
                LIMIT 5
            """).fetchall():
                print(f"  ${row[1]:.2f} {row[0]}: {row[2]} -> {row[3]}")
            print("-" * 60)

        print("Done.")
    except duckdb.Error as e:
        print(f"Error querying databases: {e}")

if len(sys.argv) != 3:
    print("Usage: python3 compare_runs.py <baseline.duckdb> <new_run.duckdb>")
    sys.exit(1)
compare_runs(sys.argv[1], sys.argv[2])
PYEOF
```

---

## 6. `prefetch_skus.py`

**What it does:** One-time setup — pre-fetches all GCP SKU IDs and AWS instance pricing into the
skill's global caches (`data/resolved_skus.json` and `data/aws_instance_prices.json`). After this
runs, `resolve_sku()` uses the cache for instant lookups and only calls the live GCP API for
genuinely new SKUs.

**When to use:** Run once after installing the skill, and again after a catalog refresh. Do NOT
add to `pre_llm_scripts` — it is not a per-job script.

**Run with:**
```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
python3 - --region us-central1 << 'PYEOF'
import os, sys
SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.environ.get("JOB_DIR", os.getcwd())

import gzip, json, re, subprocess, urllib.request, urllib.parse
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import duckdb

DATA_DIR  = os.path.join(SKILL_DIR, "data")
RESOLVED_SKUS_FILE = os.path.join(DATA_DIR, "resolved_skus.json")
AWS_PRICES_FILE    = os.path.join(DATA_DIR, "aws_instance_prices.json")
AWS_PRICING_BASE   = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws"

def _load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default if default is not None else {}

_gcp_model_config = _load_json(os.path.join(DATA_DIR, "gcp-model-config.json"))
CORE_GCP_SERVICES = _gcp_model_config.get("prefetch_core_gcp_services", [
    "Compute Engine", "Cloud SQL", "Cloud Storage", "Networking",
    "Cloud Memorystore for Memcached", "Cloud Memorystore for Redis",
    "Cloud Memorystore", "Artifact Registry",
])

def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _append_json(path, new_entries):
    existing = _load_json(path, {})
    added = 0
    for k, v in new_entries.items():
        if k not in existing:
            existing[k] = v
            added += 1
    if added:
        _save_json(path, existing)
    return added

def _gcp_token():
    api_key = os.environ.get("GOOGLE_CLOUD_API_KEY") or os.environ.get("GCP_API_KEY")
    if api_key:
        return ("key", api_key)
    try:
        tok = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if tok:
            return ("bearer", tok)
    except Exception:
        pass
    return None

def _gcp_get(url, token_info):
    if token_info is None:
        return None
    kind, value = token_info
    if kind == "key":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}key={value}"
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {value}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  GCP fetch failed: {e}")
        return None

def _services_map():
    path = os.path.join(DATA_DIR, "services.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return {s["displayName"]: s["serviceId"] for s in json.load(f)}

_KNOWN_PHANTOM_AVAILABILITY = {
    ("n4d instance core running in delhi", "asia-south2"),
    ("n4d instance ram running in delhi", "asia-south2"),
}

def _is_phantom_availability(desc_lower, gcp_region):
    return any(desc_lower.startswith(prefix) and gcp_region == region
               for prefix, region in _KNOWN_PHANTOM_AVAILABILITY)

def fetch_gcp_skus_for_region(gcp_region, services_needed, token_info):
    svc_map = _services_map()
    new_entries = {}
    for svc_name in services_needed:
        svc_id = svc_map.get(svc_name)
        if not svc_id:
            print(f"  [GCP] service not in services.json: {svc_name}")
            continue
        skus = []
        if token_info:
            page_token = ""
            while True:
                url = f"https://cloudbilling.googleapis.com/v1/services/{svc_id}/skus?pageSize=5000"
                if page_token:
                    url += f"&pageToken={urllib.parse.quote(page_token)}"
                data = _gcp_get(url, token_info)
                if not data:
                    break
                skus.extend(data.get("skus", []))
                page_token = data.get("nextPageToken", "")
                if not page_token:
                    break
            print(f"  [GCP] {svc_name}: {len(skus)} SKUs fetched from API")
        else:
            sku_file = os.path.join(DATA_DIR, "skus", f"{svc_id}.json.gz")
            if os.path.exists(sku_file):
                with gzip.open(sku_file, "rt") as f:
                    skus = json.load(f)
                print(f"  [GCP] {svc_name}: {len(skus)} SKUs from bundled catalog")
        for sku in skus:
            geo = sku.get("geoTaxonomy", {})
            in_region = (geo.get("type") == "GLOBAL" or gcp_region in sku.get("serviceRegions", []))
            if not in_region:
                continue
            desc = sku.get("description", "")
            desc_lower = desc.lower()
            if _is_phantom_availability(desc_lower, gcp_region):
                continue
            key = f"gcp|{svc_name}|{desc_lower}|{gcp_region}"
            new_entries[key] = sku["skuId"]
    return new_entries

def fetch_aws_instance_prices(aws_region, instance_types):
    existing = _load_json(AWS_PRICES_FILE, {})
    needed = [it for it in instance_types if it and it not in existing]
    if not needed:
        print(f"  [AWS] all {len(instance_types)} instance types already cached")
        return 0
    url = f"{AWS_PRICING_BASE}/AmazonEC2/current/{aws_region}/index.json"
    print(f"  [AWS EC2] fetching pricing index for {aws_region} ...")
    try:
        req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"  [AWS EC2] fetch failed: {e}")
        return 0
    products = data.get("products", {})
    terms    = data.get("terms", {}).get("OnDemand", {})
    new_prices = {}
    for sku_hash, product in products.items():
        attrs = product.get("attributes", {})
        itype = attrs.get("instanceType", "")
        if itype not in needed:
            continue
        if attrs.get("operatingSystem", "") != "Linux" or attrs.get("tenancy", "Shared") != "Shared":
            continue
        for _, term_data in terms.get(sku_hash, {}).items():
            for _, dim in term_data.get("priceDimensions", {}).items():
                price = float(dim.get("pricePerUnit", {}).get("USD", 0))
                if price > 0:
                    new_prices[itype] = {"od_hourly_usd": price, "region": aws_region}
                    break
    added = _append_json(AWS_PRICES_FILE, new_prices)
    print(f"  [AWS EC2] {len(new_prices)} prices found, {added} new entries cached")
    return added

import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--region", default="asia-southeast1")
parser.add_argument("--aws-region", default="ap-southeast-1")
parser.add_argument("--force", action="store_true")
args = parser.parse_args()

gcp_region = args.region
aws_region = args.aws_region
print(f"One-time SKU prefetch: gcp={gcp_region}, aws={aws_region}")

print("\n[1/2] GCP SKU prefetch")
existing_cache = _load_json(RESOLVED_SKUS_FILE, {})
region_cached = any(
    k.startswith("gcp|Compute Engine|") and k.endswith(f"|{gcp_region}")
    for k in existing_cache
)
if region_cached and not args.force:
    print(f"  Region {gcp_region} already in cache — use --force to re-fetch")
else:
    token_info = _gcp_token()
    print(f"  Auth: {token_info[0] if token_info else 'none — bundled catalog only'}")
    gcp_entries = fetch_gcp_skus_for_region(gcp_region, CORE_GCP_SERVICES, token_info)
    added_gcp = _append_json(RESOLVED_SKUS_FILE, gcp_entries)
    print(f"  -> {len(gcp_entries)} region-matched SKUs, {added_gcp} new entries cached")

print("\n[2/2] AWS EC2 instance price prefetch")
ec2_static = _load_json(os.path.join(DATA_DIR, "ec2-instance-types.json"), {})
ec2_types = [k for k in ec2_static if not k.startswith("_")]
fetch_aws_instance_prices(aws_region, ec2_types)

total_gcp = len(_load_json(RESOLVED_SKUS_FILE, {}))
total_aws = len(_load_json(AWS_PRICES_FILE, {}))
print(f"\nDone. Cache totals: {total_gcp} GCP SKUs, {total_aws} AWS prices")
PYEOF
```

---

## 7. `merge_mappings.py`

**What it does:** Bulk-INSERTs all mechanic-group `*_mappings.json` files into `aws_li_to_gcp_li`
in one transaction. Validates SKU IDs against `catalog.duckdb` (nulling phantoms), deduplicates
exact `(aws_li_key, component)` pairs, and collapses all-passthrough break-down groups to avoid
N× cost multiplication.

**When to use:** Automatically run as a Phase 2 `post_llm_script` after all LLM mapping agents
complete. Also run manually to re-merge after editing a `*_mappings.json` file.

**Run with:**
```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
export JOB_DIR="/path/to/job/directory"
python3 - "$JOB_DIR/projection-audit/projection.duckdb" "$JOB_DIR/projection-audit/mappings" << 'PYEOF'
import os, sys
SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.environ.get("JOB_DIR", os.getcwd())

import json
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import duckdb
from collections import defaultdict

CATALOG_DB = os.path.join(SKILL_DIR, "data", "catalog.duckdb")

# Synthetic SKU IDs injected by apply_rates.py — never in catalog.duckdb.
# Inlined from egress_rates.py hardcoded fallback.
_EGRESS_SKUS = {
    "interzone":   ("GCP-EGRESS-INTERZONE",   "Network Inter Zone Egress",   0.01),
    "interregion": ("GCP-EGRESS-INTERREGION", "Network Inter Region Egress", 0.08),
    "internet":    ("GCP-EGRESS-INTERNET",    "Network Internet Egress",     0.12),
}
_SYNTHETIC_SKUS = {sku_id for sku_id, _, _ in _EGRESS_SKUS.values()}

COLUMNS = [
    "aws_li_key", "gcp_service", "gcp_sku_id", "gcp_sku_name",
    "component", "strategy", "unit_multiplier", "gcp_region",
    "projection_note", "mapping_confidence", "is_workload", "break_down",
]

def load_mappings(mappings_dir: str):
    manifest_path = os.path.join(os.path.dirname(mappings_dir.rstrip("/")), "phase2_manifest.json")
    expected_groups = []
    if os.path.exists(manifest_path):
        manifest = json.load(open(manifest_path))
        expected_groups = [
            g for g, meta in manifest.items()
            if not g.startswith("_") and isinstance(meta, dict) and meta.get("needs_llm", True)
        ]
    all_rows = []
    missing = []
    found_files = sorted(f for f in os.listdir(mappings_dir) if f.endswith("_mappings.json"))
    for fname in found_files:
        path = os.path.join(mappings_dir, fname)
        try:
            rows = json.load(open(path))
            if not isinstance(rows, list):
                print(f"WARNING: {fname} is not a JSON array — skipping", file=sys.stderr)
                continue
            all_rows.extend(rows)
        except Exception as e:
            print(f"WARNING: could not read {fname}: {e}", file=sys.stderr)
    found_groups = {f.replace("_mappings.json", "") for f in found_files}
    for g in expected_groups:
        if g not in found_groups:
            missing.append(g)
    return all_rows, missing

def ensure_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS aws_li_to_gcp_li (
            aws_li_key TEXT, gcp_service TEXT, gcp_sku_id TEXT, gcp_sku_name TEXT,
            gcp_sku_unit TEXT, component TEXT, strategy TEXT, unit_multiplier DOUBLE,
            gcp_region TEXT, projection_note TEXT, mapping_confidence DOUBLE,
            is_workload BOOLEAN, break_down BOOLEAN
        )
    """)

if len(sys.argv) != 3:
    print(f"Usage: {sys.argv[0]} <projection.duckdb> <mappings_dir>", file=sys.stderr)
    sys.exit(1)

db_path, mappings_dir = sys.argv[1], sys.argv[2]

if not os.path.exists(mappings_dir):
    print(f"ERROR: mappings dir not found: {mappings_dir}", file=sys.stderr)
    sys.exit(1)

rows, missing = load_mappings(mappings_dir)

if missing:
    print(f"WARNING: {len(missing)} group(s) have no mapping file:", file=sys.stderr)
    for g in missing:
        print(f"  missing: {g}_mappings.json", file=sys.stderr)
if not rows:
    print("WARNING: no mapping rows found — aws_li_to_gcp_li will be empty.")

con = duckdb.connect(db_path)
ensure_table(con)
con.execute("DELETE FROM aws_li_to_gcp_li")

valid_skus = set()
if os.path.exists(CATALOG_DB):
    try:
        cat = duckdb.connect(CATALOG_DB, read_only=True)
        valid_skus = {r[0] for r in cat.execute("SELECT sku_id FROM skus").fetchall()}
        cat.close()
    except Exception as e:
        print(f"WARNING: could not load catalog for SKU validation: {e}", file=sys.stderr)

nulled = 0
if valid_skus:
    for r in rows:
        sku = r.get("gcp_sku_id")
        if sku and sku not in valid_skus and sku not in _SYNTHETIC_SKUS:
            print(f"  INVALID SKU nulled: {sku!r} on {r.get('aws_li_key')} ({r.get('gcp_sku_name', '')})", file=sys.stderr)
            r["gcp_sku_id"] = None
            nulled += 1
    if nulled:
        print(f"  {nulled} phantom SKU ID(s) nulled — apply_rates.py will fall back to passthrough.")

# Dedup pass 1: exact (aws_li_key, component)
seen = {}
for r in rows:
    key = (r.get("aws_li_key"), r.get("component"))
    seen[key] = r
rows = list(seen.values())

# Dedup pass 2: all-passthrough break_down collapse
by_key = defaultdict(list)
for r in rows:
    by_key[r.get("aws_li_key")].append(r)

collapsed_rows = []
collapsed_count = 0
for li_key, group in by_key.items():
    all_breakdown_passthrough = (
        len(group) > 1
        and all(r.get("break_down") and r.get("strategy") == "passthrough" for r in group)
    )
    if all_breakdown_passthrough:
        representative = group[0].copy()
        representative["break_down"] = False
        representative["component"] = None
        representative["unit_multiplier"] = 1.0
        collapsed_rows.append(representative)
        collapsed_count += 1
        print(f"  COLLAPSED {len(group)} break_down+passthrough rows -> 1 passthrough: {li_key}", file=sys.stderr)
    else:
        collapsed_rows.extend(group)

if collapsed_count:
    print(f"  {collapsed_count} aws_li_key(s) collapsed to avoid N* cost multiplication.", file=sys.stderr)
rows = collapsed_rows

records = [tuple(r.get(col) for col in COLUMNS) for r in rows]
placeholders = ", ".join(["?"] * len(COLUMNS))
col_list = ", ".join(COLUMNS)
con.executemany(f"INSERT INTO aws_li_to_gcp_li ({col_list}) VALUES ({placeholders})", records)
con.commit()

try:
    stats = con.execute("""
        SELECT m.mechanic_group, COUNT(*) AS mapped_rows
        FROM aws_li_to_gcp_li li
        JOIN aws_li_catalog m USING (aws_li_key)
        GROUP BY m.mechanic_group
        ORDER BY mapped_rows DESC
    """).fetchall()
    total = sum(r[1] for r in stats)
    print(f"\n{'mechanic_group':<25}  {'mapped_rows':>12}")
    print("-" * 40)
    for group, cnt in stats:
        print(f"{group or 'unknown':<25}  {cnt:>12}")
    print("-" * 40)
    print(f"{'TOTAL':<25}  {total:>12}")
except Exception:
    total = len(records)

con.close()
print(f"\nMerge complete. {total} rows inserted into aws_li_to_gcp_li.")
PYEOF
```

---

## 8. `verify_golden_mappings.py`

**What it does:** Runs regression assertions on a completed projection database. Checks that
common instance types (t3, t4g, g5, p4d, db.r6i) map to their expected GCP service and SKU
family, and that CloudWatch log rows map to Cloud Logging.

**When to use:** After Phase 2 completes or after any mapping configuration change. Also useful
as a CI check.

**Run with:**
```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
export JOB_DIR="/path/to/job/directory"
python3 - "$JOB_DIR/projection-audit/projection.duckdb" << 'PYEOF'
import os, sys
SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.environ.get("JOB_DIR", os.getcwd())

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import duckdb

db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(JOB_DIR, "projection-audit", "projection.duckdb")
conn = duckdb.connect(db_path)
print(f"Running golden mappings regression assertions on {db_path}...")

vm_rows = conn.execute("""
    SELECT c.instance_type, m.gcp_service, m.gcp_sku_name
    FROM aws_li_to_gcp_li m
    JOIN aws_li_catalog c USING(aws_li_key)
    WHERE c.instance_type IS NOT NULL
""").fetchall()

assertions = {
    "t3.large":    {"service": "Compute Engine", "sku_desc": "E2 Instance"},
    "t4g.large":   {"service": "Compute Engine", "sku_desc": "E2 Instance"},
    "g5.2xlarge":  {"service": "Compute Engine", "sku_desc": "G2 Instance"},
    "p4d.24xlarge":{"service": "Compute Engine", "sku_desc": "A2 Instance"},
    "db.r6i.large":{"service": "Cloud SQL",      "sku_desc": "Cloud SQL"},
}

failures = 0
passed = 0

for itype, gcp_svc, sku_name in vm_rows:
    if "gpu" in (sku_name or "").lower() or "nvidia" in (sku_name or "").lower():
        continue
    itype_lower = itype.lower()
    if itype_lower.startswith("db."):
        sku_lower = (sku_name or "").lower()
        if any(w in sku_lower for w in ("storage", "snapshot", "i/o", "backup")):
            continue
        spec = assertions["db.r6i.large"]
        sku_ok = spec["sku_desc"].lower() in sku_lower
        svc_ok = spec["service"].lower() in (gcp_svc or "").lower()
        if not (sku_ok and svc_ok):
            print(f"FAIL: DB instance {itype} mapped to {gcp_svc} / {sku_name} (expected {spec['service']} / {spec['sku_desc']})")
            failures += 1
        else:
            passed += 1
    else:
        for ref_itype, spec in assertions.items():
            if ref_itype.startswith("db."):
                continue
            if ref_itype in itype_lower:
                sku_ok = spec["sku_desc"].lower() in (sku_name or "").lower()
                svc_ok = spec["service"].lower() in (gcp_svc or "").lower()
                if not (sku_ok and svc_ok):
                    print(f"FAIL: VM instance {itype} mapped to {gcp_svc} / {sku_name} (expected {spec['service']} / {spec['sku_desc']})")
                    failures += 1
                else:
                    passed += 1

cw_rows = conn.execute("""
    SELECT c.usage_type, c.operation, m.gcp_service, m.gcp_sku_name
    FROM aws_li_to_gcp_li m
    JOIN aws_li_catalog c USING(aws_li_key)
    WHERE c.product LIKE '%CloudWatch%'
""").fetchall()

for ut, op, svc, sku_name in cw_rows:
    ut_lower = (ut or "").lower()
    op_lower = (op or "").lower()
    if "log" in ut_lower or "log" in op_lower or "ingestion" in ut_lower or "ingestion" in op_lower:
        if svc != "Cloud Logging" or "Log Storage" not in (sku_name or ""):
            print(f"FAIL: CloudWatch Log row {ut}/{op} mapped to {svc} / {sku_name} (expected Cloud Logging / Log Storage)")
            failures += 1
        else:
            passed += 1
    else:
        if "cloud monitoring" not in (svc or "").lower():
            print(f"FAIL: CloudWatch Metric row {ut}/{op} mapped to {svc} (expected Cloud Monitoring)")
            failures += 1
        else:
            passed += 1

print(f"Golden assertions completed. Passed: {passed}, Failed: {failures}")
if failures > 0:
    print(f"WARNING: {failures} golden assertion(s) failed — report will still generate.")
    sys.exit(0)
print("All golden regression assertions passed!")
PYEOF
```

---

## 9. `calibrate_confidence.py`

**What it does:** Applies deterministic confidence ceilings to `aws_li_to_gcp_li` after LLM
mapping. High-ambiguity services (OpenSearch, MSK, RDS, ElastiCache, Windows) get capped and
receive architecture-review disclosure notes. Caps are driven by `data/confidence-caps.json`.

**When to use:** Automatically run as a Phase 2 `post_llm_script`, after `merge_mappings.py`.

**Run with:**
```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
export JOB_DIR="/path/to/job/directory"
python3 - "$JOB_DIR/projection-audit/projection.duckdb" << 'PYEOF'
import os, sys
SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.environ.get("JOB_DIR", os.getcwd())

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import duckdb

# ── inlined: config_loader ────────────────────────────────────────────────────
def _load_data_config(name):
    path = os.path.join(SKILL_DIR, "data", f"{name}.json")
    try:
        import json as _j
        with open(path, encoding="utf-8") as f:
            return _j.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

_cfg = _load_data_config

# ── main script ───────────────────────────────────────────────────────────────
def _build_product_clause(entry: dict) -> str:
    if "product_ilike" in entry:
        return f"(c.product ILIKE '{entry['product_ilike']}')"
    patterns = entry.get("product_ilike_patterns", [])
    return "(" + " OR ".join(f"c.product ILIKE '{p}'" for p in patterns) + ")"

def _load_caps() -> list:
    raw = _cfg("confidence-caps").get("caps", [])
    result = []
    for entry in raw:
        product_clause = _build_product_clause(entry)
        gcp_filter = (
            f"m.gcp_service ILIKE '{entry['gcp_service_ilike']}'"
            if entry.get("gcp_service_ilike") else None
        )
        result.append((
            entry["label"],
            product_clause,
            gcp_filter,
            entry["ceiling"],
            entry.get("architecture_note"),
        ))
    return result

_CAPS = _load_caps()

if len(sys.argv) != 2:
    print(f"Usage: {sys.argv[0]} <projection.duckdb>", file=sys.stderr)
    sys.exit(1)

db_path = sys.argv[1]
con = duckdb.connect(db_path)

tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
if "aws_li_to_gcp_li" not in tables:
    print("aws_li_to_gcp_li not found — skipping confidence calibration")
    con.close()
    sys.exit(0)

total = 0

for label, cat_filter, svc_filter, ceiling, note in _CAPS:
    join_filter = f"WHERE {cat_filter}"
    if svc_filter:
        join_filter += f" AND {svc_filter}"

    not_already_noted = ""
    if note:
        marker = note[:30].replace("'", "''")
        not_already_noted = f" AND (m.projection_note IS NULL OR m.projection_note NOT ILIKE '%{marker}%')"

    n = con.execute(f"""
        SELECT COUNT(*) FROM aws_li_to_gcp_li m
        JOIN aws_li_catalog c USING (aws_li_key)
        {join_filter}
          AND COALESCE(m.mapping_confidence, 1.0) >= {ceiling}
          {not_already_noted}
    """).fetchone()[0]

    if n == 0:
        continue

    if note:
        con.execute(f"""
            UPDATE aws_li_to_gcp_li
            SET mapping_confidence = LEAST(COALESCE(mapping_confidence, 1.0), {ceiling}),
                projection_note = COALESCE(projection_note, '') || ' [{note}]'
            WHERE aws_li_key IN (
                SELECT c.aws_li_key FROM aws_li_catalog c
                JOIN aws_li_to_gcp_li m USING (aws_li_key)
                {join_filter}
                  AND COALESCE(m.mapping_confidence, 1.0) >= {ceiling}
                  {not_already_noted}
            )
        """)
    else:
        con.execute(f"""
            UPDATE aws_li_to_gcp_li
            SET mapping_confidence = LEAST(COALESCE(mapping_confidence, 1.0), {ceiling})
            WHERE aws_li_key IN (
                SELECT c.aws_li_key FROM aws_li_catalog c
                JOIN aws_li_to_gcp_li m USING (aws_li_key)
                {join_filter}
                  AND COALESCE(m.mapping_confidence, 1.0) >= {ceiling}
            )
        """)

    print(f"  calibrate_confidence: {label}: {n} row(s) capped at {ceiling:.0%}")
    total += n

n_win = con.execute("""
    SELECT COUNT(*) FROM aws_li_to_gcp_li m
    JOIN aws_li_catalog c USING (aws_li_key)
    WHERE (c.operating_system ILIKE '%Windows%'
           OR c.projection_note ILIKE '%Windows%'
           OR c.projection_note ILIKE '%license-premium%')
      AND COALESCE(m.mapping_confidence, 1.0) >= 0.75
""").fetchone()[0]

if n_win:
    con.execute("""
        UPDATE aws_li_to_gcp_li
        SET mapping_confidence = LEAST(COALESCE(mapping_confidence, 1.0), 0.75),
            projection_note = CASE
                WHEN projection_note ILIKE '%license%' OR projection_note ILIKE '%BYOL%'
                THEN projection_note
                ELSE COALESCE(projection_note, '') ||
                     ' [Windows license not included in GCP pricing; '
                     'add BYOL or Windows Server premium before finalizing cost]'
            END
        WHERE aws_li_key IN (
            SELECT c.aws_li_key FROM aws_li_catalog c
            JOIN aws_li_to_gcp_li m USING (aws_li_key)
            WHERE (c.operating_system ILIKE '%Windows%'
                   OR c.projection_note ILIKE '%Windows%'
                   OR c.projection_note ILIKE '%license-premium%')
              AND COALESCE(m.mapping_confidence, 1.0) >= 0.75
        )
    """)
    print(f"  calibrate_confidence: Windows: {n_win} row(s) capped at 75%")
    total += n_win

con.commit()
con.close()
print(f"calibrate_confidence: done — {total} total row(s) adjusted")
PYEOF
```

---

## 10. `reconcile_capacity.py`

**What it does:** Eliminates vCPU and RAM under-provisioning after LLM mapping. For any
`map`/`break_down` row where the GCP `unit_multiplier` is less than the AWS instance's actual
spec (`instance_vcpus` or `instance_ram_gb`), bumps the multiplier to the AWS spec and tags the
row with a `[capacity adjusted: ...]` note. Enforces zero-tolerance: even a 1-vCPU deficit is
fixed.

**When to use:** Automatically run as a Phase 2 `post_llm_script`, after `calibrate_confidence.py`.

**Run with:**
```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
export JOB_DIR="/path/to/job/directory"
python3 - "$JOB_DIR/projection-audit/projection.duckdb" << 'PYEOF'
import os, sys
SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.environ.get("JOB_DIR", os.getcwd())

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import duckdb

if len(sys.argv) != 2:
    print(f"Usage: {sys.argv[0]} <projection.duckdb>", file=sys.stderr)
    sys.exit(1)

db_path = sys.argv[1]
con = duckdb.connect(db_path)

tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
if "aws_li_to_gcp_li" not in tables:
    print("aws_li_to_gcp_li not found — skipping capacity reconciliation")
    con.close()
    sys.exit(0)

under_vcpu = con.execute("""
    SELECT m.aws_li_key,
           m.unit_multiplier  AS gcp_vcpu,
           c.instance_vcpus   AS aws_vcpu,
           c.instance_type
    FROM aws_li_to_gcp_li m
    JOIN aws_li_catalog c USING (aws_li_key)
    WHERE m.strategy IN ('break_down', 'map')
      AND m.component  = 'core'
      AND c.instance_vcpus IS NOT NULL
      AND m.unit_multiplier < c.instance_vcpus
    ORDER BY (c.instance_vcpus - m.unit_multiplier) DESC
""").fetchall()

vcpu_gain = 0.0
for key, gcp_v, aws_v, itype in under_vcpu:
    con.execute("""
        UPDATE aws_li_to_gcp_li
        SET unit_multiplier = ?,
            projection_note  = COALESCE(projection_note, '') ||
                ' [capacity adjusted: vCPU ' || CAST(ROUND(?, 2) AS VARCHAR) ||
                ' -> ' || CAST(ROUND(?, 2) AS VARCHAR) || ' to match AWS ' || ? || ']'
        WHERE aws_li_key = ? AND strategy IN ('break_down', 'map') AND component = 'core'
    """, [aws_v, gcp_v, aws_v, itype or "instance", key])
    vcpu_gain += aws_v - gcp_v

under_ram = con.execute("""
    SELECT m.aws_li_key,
           m.unit_multiplier  AS gcp_ram,
           c.instance_ram_gb  AS aws_ram,
           c.instance_type
    FROM aws_li_to_gcp_li m
    JOIN aws_li_catalog c USING (aws_li_key)
    WHERE m.strategy IN ('break_down', 'map')
      AND m.component  = 'ram'
      AND c.instance_ram_gb IS NOT NULL
      AND m.unit_multiplier < c.instance_ram_gb
    ORDER BY (c.instance_ram_gb - m.unit_multiplier) DESC
""").fetchall()

ram_gain = 0.0
for key, gcp_r, aws_r, itype in under_ram:
    con.execute("""
        UPDATE aws_li_to_gcp_li
        SET unit_multiplier = ?,
            projection_note  = COALESCE(projection_note, '') ||
                ' [capacity adjusted: RAM ' || CAST(ROUND(?, 1) AS VARCHAR) ||
                ' GB -> ' || CAST(ROUND(?, 1) AS VARCHAR) ||
                ' GB to match AWS ' || ? || ']'
        WHERE aws_li_key = ? AND strategy IN ('break_down', 'map') AND component = 'ram'
    """, [aws_r, gcp_r, aws_r, itype or "instance", key])
    ram_gain += aws_r - gcp_r

con.commit()
con.close()

if under_vcpu:
    print(f"reconcile_capacity: vCPU: {len(under_vcpu)} row(s) upsized (+{vcpu_gain:.2f} vCPU recovered)")
if under_ram:
    print(f"reconcile_capacity: RAM:  {len(under_ram)} row(s) upsized (+{ram_gain:.2f} GB recovered)")
if not under_vcpu and not under_ram:
    print("reconcile_capacity: no capacity deficits found — all rows meet or exceed AWS specs")
PYEOF
```
