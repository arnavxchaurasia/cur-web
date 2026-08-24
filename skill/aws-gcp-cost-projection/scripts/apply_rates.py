#!/usr/bin/env python3
import duckdb
import os
import sys
import json
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from log_utils import get_logger, fatal
from projection_view import create_projection_view
from egress_rates import EGRESS_SKUS
from apply_static_mappings import GCP_COMPUTE_ENGINE, CUD_PCT_FALLBACK as _CUD_PCT_FALLBACK

log = get_logger("apply_rates")

JOB_DIR = os.getcwd()
DB_PATH = os.path.join(JOB_DIR, "projection-audit", "projection.duckdb")
DATA_DIR = os.path.join(os.environ.get("SKILL_DIR", ""), "data")
CATALOG_DB = os.path.join(DATA_DIR, "catalog.duckdb")


def load_cud_pct():
    """Load CUD multipliers from data/cud_pct.json, falling back to the dict above."""
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
    "africa": ["africa-south1"]
}

def blended_rate(tiered_rates, total_qty):
    total_cost = 0.0
    for i, tier in enumerate(tiered_rates):
        tier_start = tier.get("startUsageAmount", 0)
        tier_end = tiered_rates[i+1].get("startUsageAmount") if i+1 < len(tiered_rates) else float("inf")
        tier_qty = max(0, min(total_qty, tier_end) - tier_start)
        total_cost += tier_qty * tier.get("rate", 0)
    return total_cost / total_qty if total_qty > 0 else tiered_rates[-1].get("rate", 0)

def _score_sku_match(description: str, gcp_sku_name: str) -> int:
    """Score how well a catalog SKU description matches the LLM-provided gcp_sku_name.
    Higher = better. Uses word-level intersection — no hardcoded rules needed."""
    desc_words = set(description.lower().split())
    name_words = set(gcp_sku_name.lower().split())
    return len(desc_words & name_words)


def _resolve_sku_for_row(gcp_service, gcp_sku_name, gcp_region, *_ignored):
    """Return the best gcp_sku_id for a NULL-sku mapped row using catalog description search.

    Queries catalog.duckdb (indexed) instead of scanning gzip files. Same word-overlap
    scoring as before; same free-tier / intra-zone exclusions; same region-preference logic.
    Extra positional args are accepted but ignored (backwards-compat with old signature).
    """
    if not gcp_sku_name or not os.path.exists(CATALOG_DB):
        return None

    cat = duckdb.connect(CATALOG_DB, read_only=True)
    try:
        rows = cat.execute("""
            SELECT sku_id, description, service_regions
            FROM skus
            WHERE service_name = ? AND usage_type = 'OnDemand'
        """, [gcp_service]).fetchall()
    finally:
        cat.close()

    best_exact_score, best_exact_id = (-1, 0), None
    best_any_score,   best_any_id   = (-1, 0), None

    for sku, desc, regions in rows:
        dl = desc.lower()
        if "free tier" in dl or "promotional" in dl or "trial" in dl:
            continue
        if "intra zone" in dl or "intra-zone" in dl or "intra region" in dl or "intra-region" in dl:
            continue
        score = _score_sku_match(desc, gcp_sku_name)
        if score <= 0:
            continue
        # Composite rank: (word-overlap score, -extra words). Tighter match wins ties.
        rank = (score, -len(desc.split()))
        if gcp_region and gcp_region in (regions or []):
            if rank > best_exact_score or (rank == best_exact_score and (best_exact_id is None or sku < best_exact_id)):
                best_exact_score, best_exact_id = rank, sku
        if rank > best_any_score or (rank == best_any_score and (best_any_id is None or sku < best_any_id)):
            best_any_score, best_any_id = rank, sku

    # `best_exact_id or best_any_id` used to unconditionally prefer ANY
    # exact-region match over a global SKU, regardless of actual word-overlap
    # quality — confirmed real: for gcp_sku_name="Cloud Run Functions
    # Invocations" this picked a regional "Cloud Run functions CPU
    # (Request-based billing) in asia-south1" SKU (word-overlap score 3, a
    # completely different billing dimension — per-second CPU time, not
    # per-invocation count) over the correct GLOBAL "Cloud Run Functions
    # Invocations" SKU (score 4, an exact description match) purely because
    # the latter is scoped "global" rather than a specific region. A global
    # SKU is valid in every region by definition, so region-exactness should
    # only break a genuine tie, never override a strictly better match.
    if best_exact_id is not None and best_any_id is not None:
        return best_exact_id if best_exact_score >= best_any_score else best_any_id
    return best_exact_id or best_any_id


import re as _re_mod
# Only Inferentia/Trainium/DL-AMI have no GCP equivalent at all — these stay as passthrough.
# NVIDIA GPU families (g*, p*) ARE mapped by family_mapper.py to G2/A2/A3+GPU components;
# those mappings are intentionally NOT overridden here.
_ACCEL_RE = _re_mod.compile(r'\b(inf\d|trn\d|dl\d|vt\d)[a-z0-9]*\.')


def enforce_accelerator_passthrough(conn):
    """AI accelerators / GPUs (Inferentia, Trainium, GPU families) must NEVER be
    priced as a CPU VM. Deterministically collapse any such row's mapping to a
    single passthrough (manual-review) row — overriding whatever Phase 2/5 did.
    UPDATE-only (no delete): keep the first component row as passthrough, set the
    rest to ignore, so cost = AWS parity once (no break_down double-count)."""
    rows = conn.execute("""
        SELECT aws_li_key, COALESCE(operation,'')||' '||COALESCE(instance_type,'') sig
        FROM aws_li_catalog WHERE is_workload
    """).fetchall()
    accel = [k for k, sig in rows
             if "inferentia" in sig.lower() or "trainium" in sig.lower() or _ACCEL_RE.search(sig.lower())]
    fixed = 0
    for k in accel:
        comps = conn.execute(
            "SELECT rowid, strategy FROM aws_li_to_gcp_li WHERE aws_li_key = ? ORDER BY rowid", [k]
        ).fetchall()
        if not comps or all(s == "passthrough" for _, s in comps):
            continue
        keep = comps[0][0]
        conn.execute("UPDATE aws_li_to_gcp_li SET strategy='ignore', unit_multiplier=0, "
                     "projection_note='accelerator component folded into passthrough' "
                     "WHERE aws_li_key=? AND rowid<>?", [k, keep])
        conn.execute("UPDATE aws_li_to_gcp_li SET strategy='passthrough', "
                     "gcp_service='Manual Sizing Required', gcp_sku_id=NULL, gcp_sku_name=NULL, "
                     "unit_multiplier=NULL, mapping_confidence=0.3, component='accelerator', "
                     "projection_note='AI accelerator (Inferentia/Trainium/GPU) — Manual sizing required (A3/A2/G2/L4/H100/etc.)' "
                     "WHERE aws_li_key=? AND rowid=?", [k, keep])
        fixed += 1
    if fixed:
        log.info(f"Enforced accelerator passthrough on {fixed} row(s)")
    return fixed


_LICENSE_MARKER = "[license-premium-not-modeled]"


def remap_elasticache_to_memorystore(conn):
    """ElastiCache (cache) must map to Memorystore, not Cloud SQL. The LLM often
    mis-picks Cloud SQL's 'Custom Core/RAM' SKUs; Memorystore for Memcached has
    identically-named 'Custom Core/RAM' SKUs with the same vCPU+RAM break_down
    model, so we relabel the service and clear the sku_id — apply_rates then
    re-resolves the Memorystore SKU by the same name. Deterministic, no rate table."""
    n = conn.execute("""
        SELECT COUNT(*) FROM aws_li_to_gcp_li m JOIN aws_li_catalog cat USING (aws_li_key)
        WHERE LOWER(cat.product) LIKE '%elasticache%' AND m.gcp_service = 'Cloud SQL'
    """).fetchone()[0]
    if not n:
        return 0
    conn.execute("""
        UPDATE aws_li_to_gcp_li SET
            gcp_service = 'Cloud Memorystore for Memcached',
            gcp_sku_id = NULL,
            projection_note = 'ElastiCache → Memorystore for Memcached (cache workload; not Cloud SQL)'
        WHERE aws_li_key IN (
            SELECT cat.aws_li_key FROM aws_li_catalog cat
            WHERE LOWER(cat.product) LIKE '%elasticache%'
        ) AND gcp_service = 'Cloud SQL'
    """)
    log.info(f"Remapped {n} ElastiCache row(s) to Memorystore for Memcached")
    return n


def fix_managed_db_storage_rows(conn, *_ignored):
    """Aurora/RDS 'Storage and I/O' rows are billed in GB-Mo, not Hrs. The LLM
    maps them to vCPU/RAM SKUs (correct for instance rows), which multiplies
    millions of GB against $/vCPU-hr and blows the total by 10,000x.

    Detect any managed-DB row where pricing_unit is not hours and the current
    SKU is a vCPU/RAM SKU, then remap to the correct Cloud SQL SSD Storage SKU.
    This is the same class of fix as enforce_accelerator_passthrough — a unit
    mismatch the LLM cannot reliably avoid.

    Two follow-on bugs in the original version of this fix, both now closed:
      - It updated every matching row (core AND ram) independently via
        `WHERE aws_li_key = ?` with no component filter, but never reset
        unit_multiplier — leaving the OLD vCPU-count/RAM-GiB-count multiplier
        in place against a $/GB-mo storage rate, and billing the same storage
        SKU twice (once per leftover component). Now: keep only the 'core'
        component as a single 'storage' row with unit_multiplier=1.0 (rate
        already applies per GB-mo of c.total_usage), and delete the 'ram' row.
      - It always picked the Zonal storage tier, even for Multi-AZ (AWS's HA
        deployment), which should map to Cloud SQL's Regional (HA) storage
        tier. Now checks usage_type for "Multi-AZ".
    """

    # Aurora Serverless v2 ACU-hour rows (usage_type contains "ServerlessV2" or
    # ":ACU") are legitimately mapped to a vCPU/RAM SKU by map_managed_db() and
    # must never be caught here. The pricing_unit exclusion below used to be the
    # only guard against that ('ACU-Hrs'/'ACU-hours' excluded) — but ingest.py
    # leaves pricing_unit as an empty string ('') for these rows rather than
    # 'ACU-Hrs', and '' is neither NULL nor literally in the exclusion list, so
    # it slipped through and this function silently overwrote a CORRECT compute
    # mapping with an incorrect storage one. Excluding by usage_type directly is
    # the real fix — it doesn't depend on pricing_unit being populated correctly.
    rows = conn.execute("""
        SELECT m.aws_li_key, m.component, c.gcp_region,
               c.product, c.usage_type
        FROM aws_li_to_gcp_li m
        JOIN aws_li_catalog c USING (aws_li_key)
        WHERE m.strategy = 'map'
          AND m.gcp_service = 'Cloud SQL'
          AND (m.gcp_sku_name ILIKE '%vCPU%' OR m.gcp_sku_name ILIKE '% RAM%'
               OR m.gcp_sku_name ILIKE '%Core%')
          AND c.pricing_unit NOT IN ('Hrs', 'hours', 'Hour', 'ACU-Hrs', 'ACU-hours', '')
          AND c.pricing_unit IS NOT NULL
          AND c.usage_type NOT ILIKE '%ServerlessV2%'
          AND c.usage_type NOT ILIKE '%:ACU%'
          AND c.line_item_type NOT IN ('DiscountedUsage', 'SavingsPlanCoveredUsage')
          AND (c.product ILIKE '%Aurora%' OR c.product ILIKE '%RDS%'
               OR c.product ILIKE '%Relational%')
    """).fetchall()

    if not rows:
        return 0

    # Group by aws_li_key — a single AWS storage/IO line item can carry both
    # 'core' and 'ram' component rows from the original (wrong) vCPU/RAM mapping.
    by_key = {}
    for key, component, gcp_region, product, usage_type in rows:
        by_key.setdefault(key, {"components": [], "gcp_region": gcp_region,
                                 "product": product, "usage_type": usage_type})
        by_key[key]["components"].append(component)

    fixed = 0
    for key, info in by_key.items():
        gcp_region, product, usage_type = info["gcp_region"], info["product"], info["usage_type"]
        is_multi_az = "multi-az" in (usage_type or "").lower()
        tier = "Regional" if is_multi_az else "Zonal"

        # Determine storage engine from product name for SKU precision
        if "postgresql" in (product or "").lower() or "aurora" in (product or "").lower():
            storage_sku = f"Cloud SQL for PostgreSQL: {tier} - SSD storage"
        elif "mysql" in (product or "").lower():
            storage_sku = f"Cloud SQL for MySQL: {tier} - SSD storage"
        else:
            storage_sku = f"Cloud SQL: {tier} - SSD storage"

        new_sku_id = _resolve_sku_for_row("Cloud SQL", storage_sku, gcp_region)

        # Collapse to a single 'storage' component row — delete any extras
        # (e.g. the leftover 'ram' row) so storage is never billed twice.
        keep_component = info["components"][0]
        extra_components = info["components"][1:]
        if extra_components:
            placeholders = ",".join(["?"] * len(extra_components))
            conn.execute(
                f"DELETE FROM aws_li_to_gcp_li WHERE aws_li_key = ? AND component IN ({placeholders})",
                [key] + extra_components,
            )

        if not new_sku_id:
            # Fallback: passthrough rather than keep the wrong vCPU SKU
            conn.execute("""
                UPDATE aws_li_to_gcp_li SET
                    strategy = 'passthrough',
                    gcp_sku_id = NULL, gcp_sku_name = NULL,
                    component = 'storage',
                    unit_multiplier = 1.0,
                    mapping_confidence = 0.6,
                    projection_note = 'Aurora/RDS Storage+IO row — unit mismatch with vCPU SKU; '
                                      'passthrough at cost parity until Cloud SQL storage SKU resolved'
                WHERE aws_li_key = ? AND component = ?
            """, [key, keep_component])
        else:
            conn.execute("""
                UPDATE aws_li_to_gcp_li SET
                    gcp_sku_id = ?,
                    gcp_sku_name = ?,
                    component = 'storage',
                    unit_multiplier = 1.0,
                    mapping_confidence = 0.85,
                    projection_note = ?
                WHERE aws_li_key = ? AND component = ?
            """, [new_sku_id, storage_sku,
                  f"Aurora/RDS Storage+IO → {storage_sku} (unit-mismatch fix: was vCPU/RAM SKU"
                  + (", Multi-AZ→Regional tier)" if is_multi_az else ")"),
                  key, keep_component])
        fixed += 1

    if fixed:
        log.info(f"Fixed {fixed} managed-DB storage row(s) mis-mapped to vCPU SKUs")
    return fixed


def flag_license_exposure(conn):
    """Flag Windows / SQL Server / Oracle rows so a commercial-license bill is
    never SILENTLY under-projected. GCP compute here is priced license-EXCLUSIVE
    (no Windows/SQL Server premium modeled), so we cap confidence and stamp a
    projection_note. Runs after the Phase-5 LLM (so it can't be clobbered) and
    in Phase 4. Idempotent via the marker guard. Deterministic — no rate table."""
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
    if flagged:
        log.info(f"Flagged {flagged} license-exposed row(s) — confidence capped")
    return flagged


def _write_progress(phase_name="Rate-Card Fill", note="Fetching GCP rates via script"):
    try:
        with open(os.path.join(JOB_DIR, "progress.json"), "w") as f:
            json.dump({"phase": 4, "phase_name": phase_name, "last_activity": note}, f)
    except Exception as e:
        log.warning(f"Could not write progress.json: {e}")


def _write_rate_gaps(gaps):
    """Write rate-fill-gaps.md listing unreachable SKUs for Phase 5."""
    if not gaps:
        return
    out = os.path.join(JOB_DIR, "projection-audit", "rate-fill-gaps.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("# Rate-Fill Gaps\n\n")
        f.write("Rows moved to `outlier_triage` because no reachable rate was found "
                "for their region. Phase 5 owns resolution.\n\n")
        f.write("| gcp_service | gcp_region | gcp_sku_id | unreachable_rows |\n")
        f.write("|---|---|---|---|\n")
        for svc, region, sku_id, count in gaps:
            f.write(f"| {svc} | {region} | {sku_id} | {count} |\n")
    log.info(f"Wrote rate-fill-gaps.md ({len(gaps)} gap(s))")


def main():
    _write_progress()

    if not os.path.exists(DB_PATH):
        fatal(log, "projection.duckdb not found — cannot fill rates", phase=4)
        return

    conn = duckdb.connect(DB_PATH)

    # Deterministic mapping-correctness enforcement (runs before SKU resolution
    # so corrected rows get the right rate). Both override the LLM's choices:
    #   - accelerators (Inferentia/Trainium/GPU) → single passthrough, never CPU VM
    #   - ElastiCache → Memorystore for Memcached, never Cloud SQL
    enforce_accelerator_passthrough(conn)
    remap_elasticache_to_memorystore(conn)
    fix_managed_db_storage_rows(conn)

    # Add rate_source and gcp_sku_unit columns if not yet present (idempotent — safe to re-run)
    try:
        conn.execute("ALTER TABLE aws_li_to_gcp_li ADD COLUMN rate_source VARCHAR")
    except Exception:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE aws_li_to_gcp_li ADD COLUMN gcp_sku_unit VARCHAR")
    except Exception:
        pass  # column already exists

    # Snapshot rows whose sku_id was already pinned before apply_rates ran.
    # These come from static mappers (resolve_sku) or from LLM-provided exact IDs.
    # They get rate_source='exact_sku'. Rows still NULL here are word-overlap resolved.
    pinned_keys = set(
        r[0] for r in conn.execute(
            "SELECT aws_li_key FROM aws_li_to_gcp_li WHERE gcp_sku_id IS NOT NULL AND strategy IN ('map','break_down')"
        ).fetchall()
    )

    # Auto-resolve NULL gcp_sku_id for mapped rows using catalog lookup rules.
    # This prevents Phase 5 from seeing NULL projected cost and wrongly setting passthrough.
    null_sku_rows = conn.execute("""
        SELECT m.aws_li_key, m.gcp_service, m.gcp_sku_name, c.gcp_region
        FROM aws_li_to_gcp_li m
        JOIN aws_li_catalog c USING (aws_li_key)
        WHERE m.gcp_sku_id IS NULL
          AND m.strategy IN ('map', 'break_down')
    """).fetchall()

    resolved = 0
    word_overlap_keys = set()
    for aws_li_key, gcp_service, gcp_sku_name, gcp_region in null_sku_rows:
        sku_id = _resolve_sku_for_row(gcp_service, gcp_sku_name, gcp_region)
        if sku_id:
            conn.execute(
                "UPDATE aws_li_to_gcp_li SET gcp_sku_id = ? WHERE aws_li_key = ? AND gcp_sku_id IS NULL",
                (sku_id, aws_li_key),
            )
            word_overlap_keys.add(aws_li_key)
            resolved += 1
            log.debug(f"Resolved SKU: {gcp_service} / {gcp_sku_name!r} -> {sku_id}")

    if resolved:
        log.info(f"Auto-resolved {resolved} NULL gcp_sku_id row(s) via catalog word-overlap")

    # Get all required SKUs (including any just resolved above)
    skus_used = conn.execute("""
        SELECT DISTINCT m.gcp_sku_id, m.gcp_service
        FROM aws_li_to_gcp_li m
        WHERE m.gcp_sku_id IS NOT NULL
    """).fetchall()

    if not skus_used:
        log.info("No SKUs to fill rates for — nothing to do")
        return

    # Clear existing rate table just in case
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gcp_sku_rates (
            gcp_sku_id      VARCHAR,
            gcp_service     VARCHAR,
            gcp_sku_name    VARCHAR,
            resource_family VARCHAR,
            resource_group  VARCHAR,
            pricing_type    VARCHAR,
            region          VARCHAR,
            unit            VARCHAR,
            rate_usd        DOUBLE,
            source          VARCHAR,
            audit_url       VARCHAR,
            PRIMARY KEY (gcp_sku_id, pricing_type, region)
        )
    """)
    conn.execute("DELETE FROM gcp_sku_rates")

    # Load all needed SKU rates from catalog.duckdb in one indexed batch query.
    # Replaces per-SKU gzip file scanning (O(N) per lookup) with a single JOIN
    # (O(log N) via sku_id index). Both Preemptible and Commit* rows are fetched
    # now so the CUD synthesis below can fill any gaps with percentage fallbacks.
    if not os.path.exists(CATALOG_DB):
        # No catalog at all — convert every mapped row to passthrough and surface the gap.
        log.warning(f"catalog.duckdb not found at {CATALOG_DB} — all mapped rows set to passthrough")
        conn.execute("""
            UPDATE aws_li_to_gcp_li
            SET strategy = 'passthrough',
                projection_note = COALESCE(projection_note || ' ', '') ||
                    '[no-catalog: catalog.duckdb missing — refresh with scripts/refresh-catalog.sh]'
            WHERE strategy IN ('map', 'break_down')
        """)
        count = conn.execute(
            "SELECT COUNT(*) FROM aws_li_to_gcp_li WHERE strategy = 'passthrough' "
            "AND projection_note LIKE '%no-catalog%'"
        ).fetchone()[0]
        _write_rate_gaps([("ALL", "ALL", "N/A — catalog.duckdb missing", count)])
        create_projection_view(conn)
        log.info(f"Rate fill skipped — {count} row(s) set to passthrough. "
              f"Run: bash scripts/refresh-catalog.sh  to rebuild the catalog.")
        return

    sku_ids = [sku_id for sku_id, _ in skus_used]
    # Map sku_id → gcp_service as declared in mappings (for the INSERT below).
    sku_to_service = {sku_id: svc for sku_id, svc in skus_used}

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

    # Group tiers per (sku_id, usage_type); keep one metadata record per key.
    sku_tiers: dict[tuple, list] = defaultdict(list)
    sku_meta: dict[tuple, tuple] = {}
    for sku_id, svc_name, desc, rf, rg, ut, unit, regions, tier_start, rate_usd in catalog_rows:
        key = (sku_id, ut)
        sku_tiers[key].append({"startUsageAmount": tier_start, "rate": rate_usd})
        if key not in sku_meta:
            sku_meta[key] = (svc_name, desc, rf, rg, unit, regions or [])

    # Pre-fetch max usage per SKU for tiered-rate blending (one query, all SKUs).
    max_usage_rows = conn.execute("""
        SELECT m.gcp_sku_id, MAX(c.total_usage)
        FROM aws_li_to_gcp_li m
        JOIN aws_li_catalog c USING (aws_li_key)
        WHERE m.gcp_sku_id IS NOT NULL
        GROUP BY m.gcp_sku_id
    """).fetchall()
    max_usage: dict[str, float] = {r[0]: (r[1] or 0.0) for r in max_usage_rows}

    # Synthetic egress SKU IDs are injected directly into gcp_sku_rates below —
    # they are never in catalog.duckdb, so exclude them from the "not found" report.
    synthetic_ids = {sku_id for sku_id, _, _ in EGRESS_SKUS.values()}
    found_skus = {k[0] for k in sku_tiers}
    for sku_id in sku_ids:
        if sku_id not in found_skus and sku_id not in synthetic_ids:
            log.warning(f"SKU {sku_id} ({sku_to_service.get(sku_id)}) not found in catalog.duckdb — skipping")

    # Insert rates for all pricing types fetched from catalog.
    # usage_type in catalog maps 1:1 to pricing_type in gcp_sku_rates.
    for (sku_id, ut), tiers in sku_tiers.items():
        gcp_service = sku_to_service.get(sku_id, sku_meta[(sku_id, ut)][0])
        _, desc, rf, rg, unit, regions = sku_meta[(sku_id, ut)]

        if len(tiers) > 1:
            base_rate = blended_rate(tiers, max_usage.get(sku_id, 0.0))
        else:
            base_rate = tiers[0]["rate"]

        # Expand container codes (e.g. 'us' → list of us-* regions).
        expanded: set[str] = set()
        for r in regions:
            if r.lower() in CONTAINER_CODES:
                expanded.update(CONTAINER_CODES[r.lower()])
            else:
                expanded.add(r)

        # ON CONFLICT DO NOTHING: container-code expansion can produce overlapping
        # regions; the same sku_id may appear in multiple catalog rows.
        for region in expanded:
            conn.execute("""
                INSERT INTO gcp_sku_rates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
            """, (sku_id, gcp_service, desc, rf, rg, ut, region, unit, base_rate,
                  "catalog.duckdb", f"catalog.duckdb#{sku_id}"))
            
    # Unit consistency check: warn when the unit_multiplier in the mapping assumes
    # a different billing unit than the actual SKU in the rate catalog.
    # E.g., a mapper sets unit_multiplier=vcpus thinking SKU is billed per-core-hour (h),
    # but the actual SKU unit is GiBy.mo — the projected cost would be nonsense.
    try:
        unit_mismatches = conn.execute("""
            SELECT m.aws_li_key, m.gcp_sku_name, m.gcp_sku_unit, r.unit,
                   c.product, m.unit_multiplier
            FROM aws_li_to_gcp_li m
            JOIN aws_li_catalog c USING (aws_li_key)
            JOIN gcp_sku_rates r ON r.gcp_sku_id = m.gcp_sku_id
                                AND r.pricing_type = 'OnDemand'
                                AND r.region = m.gcp_region
            WHERE m.gcp_sku_unit IS NOT NULL
              AND m.gcp_sku_unit != r.unit
            LIMIT 20
        """).fetchall()
        for li_key, sku_name, mapped_unit, rate_unit, product, mult in unit_mismatches:
            log.warning(f"Unit mismatch: {product} / {sku_name}: "
                  f"mapper assumed '{mapped_unit}', SKU actually billed per '{rate_unit}' "
                  f"(unit_multiplier={mult})")
    except Exception as e:
        log.warning(f"Unit consistency check failed: {e}")

    # CUD synthesis.
    # First check how many real Commit rows came from catalog vs. need synthesis.
    real_cud_count = conn.execute(
        "SELECT COUNT(*) FROM gcp_sku_rates WHERE pricing_type IN ('Commit1Yr','Commit3Yr') "
        "AND source = 'catalog.duckdb'"
    ).fetchone()[0]
    if real_cud_count:
        log.info(f"CUD: {real_cud_count} real Commit1Yr/3Yr rows loaded from catalog")
    else:
        log.info("CUD: no real Commit rows in catalog — synthesizing from OnDemand × cud_pct.json")

    # Only synthesize CUD for services that actually appear in this bill's mappings.
    # Avoids generating thousands of unused CUD rows for services not in the bill.
    mapped_services = {r[0] for r in conn.execute(
        "SELECT DISTINCT gcp_service FROM aws_li_to_gcp_li WHERE strategy IN ('map','break_down')"
    ).fetchall()}

    # cud_pct.json is the SINGLE source of truth for which services have CUDs and
    # at what multipliers. Adding a new CUD-eligible service requires only a new entry
    # in data/cud_pct.json — no Python change here.
    #
    # Compute Engine is special: only CPU/RAM/GPU resource_groups are committable.
    # The fallback block below catches ARM/T2A and other non-standard resource_groups.
    cud_pct = load_cud_pct()
    _default_pct = cud_pct.get("DEFAULT", (0.75, 0.60))

    synthesized_cud = 0
    for svc, (r1, r3) in cud_pct.items():
        if svc == "DEFAULT":
            continue
        # Skip services not present in this bill — no point generating unused rows.
        if svc not in mapped_services and svc != GCP_COMPUTE_ENGINE:
            continue
        rg_clause = "AND resource_group IN ('CPU','RAM','GPU')" if svc == GCP_COMPUTE_ENGINE else ""
        for pricing_type, mult in [("Commit1Yr", r1), ("Commit3Yr", r3)]:
            n = conn.execute(f"""
                INSERT INTO gcp_sku_rates
                SELECT gcp_sku_id, gcp_service, gcp_sku_name, resource_family,
                       resource_group, '{pricing_type}', region, unit,
                       rate_usd * ?, 'doc-percentage', 'cud_pct.json'
                FROM gcp_sku_rates
                WHERE gcp_service = ? AND pricing_type = 'OnDemand'
                  {rg_clause}
                ON CONFLICT DO NOTHING
            """, [mult, svc]).rowcount
            synthesized_cud += (n or 0)
    if synthesized_cud:
        log.info(f"CUD synthesis: {synthesized_cud} row(s) generated from cud_pct.json percentages")

    # Compute Engine CUD fallback: catch any CPU/RAM/GPU SKUs that missed the main
    # pass above (e.g. ARM families with non-standard SKU name patterns). Restrict
    # strictly to resource_group IN ('CPU','RAM','GPU') — Hyperdisk capacity/IOPS/
    # throughput, PD snapshots, External IP, and networking SKUs must never receive
    # a synthesized CUD row; GCP does not offer committed use on those products.
    r1_ce, r3_ce = cud_pct.get(GCP_COMPUTE_ENGINE, _default_pct)
    for pricing_type, mult in [("Commit1Yr", r1_ce), ("Commit3Yr", r3_ce)]:
        conn.execute(f"""
            INSERT INTO gcp_sku_rates
            SELECT gcp_sku_id, gcp_service, gcp_sku_name, resource_family,
                   resource_group, '{pricing_type}', region, unit,
                   rate_usd * ?, 'doc-percentage-fallback', 'cud_pct.json'
            FROM gcp_sku_rates
            WHERE gcp_service = 'Compute Engine' AND pricing_type = 'OnDemand'
              AND resource_group IN ('CPU', 'RAM', 'GPU')
              AND NOT EXISTS (
                  SELECT 1 FROM gcp_sku_rates r2
                  WHERE r2.gcp_sku_id = gcp_sku_rates.gcp_sku_id
                    AND r2.pricing_type = '{pricing_type}'
                    AND r2.region = gcp_sku_rates.region
              )
            ON CONFLICT DO NOTHING
        """, [mult])

    # Preemptible rate synthesis for Compute Engine CPU/RAM SKUs.
    # GCP Preemptible (and Spot VM) price ≈ 22% of On-Demand in most regions.
    # Synthesised here so the gcp_projection VIEW can select the correct rate for
    # rows where pricing_model = 'Spot'.
    conn.execute("""
        INSERT INTO gcp_sku_rates
        SELECT gcp_sku_id, gcp_service, gcp_sku_name, resource_family,
               resource_group, 'Preemptible', region, unit,
               rate_usd * 0.22, 'preemptible-factor',
               'https://cloud.google.com/compute/docs/instances/preemptible'
        FROM gcp_sku_rates
        WHERE gcp_service = 'Compute Engine' AND pricing_type = 'OnDemand'
          AND resource_group IN ('CPU', 'RAM', 'GPU')
        ON CONFLICT DO NOTHING
    """)

    # Global fallback: for every SKU that has regional rates but no 'global' row,
    # synthesize a 'global' row by averaging the regional rates. This makes the
    # gcp_projection VIEW resilient to NULL gcp_region in aws_li_catalog — the
    # COALESCE(regional, global) fallback in the VIEW will always find a rate.
    conn.execute("""
        INSERT INTO gcp_sku_rates
        SELECT r.gcp_sku_id, r.gcp_service, r.gcp_sku_name, r.resource_family,
               r.resource_group, r.pricing_type, 'global', r.unit,
               AVG(r.rate_usd), 'global-fallback', MIN(r.audit_url)
        FROM gcp_sku_rates r
        WHERE r.region != 'global'
        GROUP BY r.gcp_sku_id, r.gcp_service, r.gcp_sku_name, r.resource_family,
                 r.resource_group, r.pricing_type, r.unit
        HAVING NOT EXISTS (
            SELECT 1 FROM gcp_sku_rates g
            WHERE g.gcp_sku_id = r.gcp_sku_id
              AND g.pricing_type = r.pricing_type
              AND g.region = 'global'
        )
        ON CONFLICT DO NOTHING
    """)

    # Inject canonical network-egress rates (deterministic, by direction) so the
    # data_transfer mappings resolve to a stable, correct $/GB instead of a
    # fuzzy catalog SKU whose rate swings 2x-8x run-to-run. 'global' region so
    # the VIEW's COALESCE(regional, global) always finds them.
    for _sku_id, _sku_name, _rate in EGRESS_SKUS.values():
        conn.execute("""
            INSERT INTO gcp_sku_rates VALUES
            (?, 'Compute Engine', ?, 'Network', 'Egress', 'OnDemand', 'global', 'gibibyte', ?, 'canonical-egress', 'published GCP egress list')
            ON CONFLICT DO NOTHING
        """, (_sku_id, _sku_name, _rate))

    # Flag commercial-license rows (Windows/SQL Server/Oracle) so they are never
    # silently under-projected — confidence capped + note stamped.
    flag_license_exposure(conn)

    # Create the projection VIEW now that rates exist, so the Phase-4 gate
    # (no_null_projected_cost) and the validator autofix can query it. Phase 5's
    # detect_outliers.py re-creates it idempotently from the same shared SQL.
    create_projection_view(conn)

    # Safety net: any mapped row still lacking an OnDemand rate after fill
    # (e.g. resolved to a Preemptible-only SKU, or SKU genuinely absent from catalog)
    # must not block the gate. Convert to passthrough so the report always generates.
    null_keys = conn.execute("""
        SELECT p.aws_li_key
        FROM gcp_projection p
        WHERE p.strategy IN ('map', 'break_down')
          AND p.gcp_projected_cost IS NULL
          AND p.aws_amortized_cost > 1
    """).fetchall()
    if null_keys:
        keys = [k[0] for k in null_keys]
        placeholders = ",".join(["?" for _ in keys])
        conn.execute(
            f"""
            UPDATE aws_li_to_gcp_li
            SET strategy = 'passthrough',
                projection_note = COALESCE(projection_note || ' ', '') ||
                    '[no-rate-fallback: passthrough at cost parity — OnDemand rate missing for resolved SKU]'
            WHERE aws_li_key IN ({placeholders})
            """,
            keys,
        )
        create_projection_view(conn)
        log.info(f"Rate-gap fallback: {len(keys)} NULL-cost row(s) set to passthrough")

    # ── Populate rate_source ───────────────────────────────────────────────────
    # Order matters: no_rate first (subset of passthrough), then passthrough,
    # then exact_sku, then word_overlap. Remaining unknowns → 'unknown'.
    #
    # 'passthrough' and 'ignore' are NOT the same thing and must not share a
    # rate_source label. strategy='passthrough' means "no reliable GCP
    # equivalent exists — AWS cost carried as a placeholder, needs manual
    # sizing." strategy='ignore' means the row was deliberately excluded from
    # GCP pricing on purpose (negative-cost credits/refunds, Savings Plan
    # commitment artifacts already reflected in amortized cost, RDS IOPS fees
    # bundled into Cloud SQL's storage price, etc.) — there is nothing to size
    # and nothing wrong with these rows. Tagging both 'passthrough' previously
    # made the report's "N passthrough rows — no reliable GCP equivalent;
    # manual sizing required" caption apply to every ignore row too, wildly
    # overstating how many rows actually need manual review.
    conn.execute("""
        UPDATE aws_li_to_gcp_li
        SET rate_source = 'no_rate'
        WHERE projection_note LIKE '%no-rate-fallback%'
    """)
    conn.execute("""
        UPDATE aws_li_to_gcp_li
        SET rate_source = 'passthrough'
        WHERE strategy = 'passthrough' AND rate_source IS NULL
    """)
    conn.execute("""
        UPDATE aws_li_to_gcp_li
        SET rate_source = 'ignored'
        WHERE strategy = 'ignore' AND rate_source IS NULL
    """)
    if pinned_keys:
        conn.execute(f"""
            UPDATE aws_li_to_gcp_li
            SET rate_source = 'exact_sku'
            WHERE strategy IN ('map','break_down') AND rate_source IS NULL
              AND aws_li_key IN ({','.join(['?']*len(pinned_keys))})
        """, list(pinned_keys))
    if word_overlap_keys:
        conn.execute(f"""
            UPDATE aws_li_to_gcp_li
            SET rate_source = 'word_overlap'
            WHERE rate_source IS NULL
              AND aws_li_key IN ({','.join(['?']*len(word_overlap_keys))})
        """, list(word_overlap_keys))
    conn.execute("UPDATE aws_li_to_gcp_li SET rate_source = 'unknown' WHERE rate_source IS NULL")

    # Normalize gcp_service to match the catalog's gcp_service field for all mapped rows.
    # The LLM often writes shortened names ("Cloud KMS", "Memorystore", "Cloud Run") that
    # differ from the catalog's canonical name ("Cloud Key Management Service (KMS)", etc.).
    # detect_outliers query D flags these mismatches. Fixing here avoids 700+ false positives.
    norm_result = conn.execute("""
        UPDATE aws_li_to_gcp_li m
        SET gcp_service = (
            SELECT r.gcp_service FROM gcp_sku_rates r
            WHERE r.gcp_sku_id = m.gcp_sku_id
            LIMIT 1
        )
        WHERE m.strategy IN ('map', 'break_down')
          AND m.gcp_sku_id IS NOT NULL
          AND m.gcp_service != (
            SELECT r.gcp_service FROM gcp_sku_rates r
            WHERE r.gcp_sku_id = m.gcp_sku_id
            LIMIT 1
          )
    """)
    norm_count = conn.execute("""
        SELECT COUNT(*) FROM aws_li_to_gcp_li m
        WHERE m.strategy IN ('map', 'break_down')
          AND m.gcp_sku_id IS NOT NULL
          AND m.gcp_service != (
            SELECT r.gcp_service FROM gcp_sku_rates r
            WHERE r.gcp_sku_id = m.gcp_sku_id LIMIT 1
          )
    """).fetchone()[0]
    if norm_count:
        log.info(f"Normalized gcp_service for {norm_count} rows to match SKU catalog")

    # Sanity check: any mapped row whose SKU has SOME rate rows but none reachable
    # for its specific region silently projects $0. Catch and route to outlier_triage.
    unreachable = conn.execute("""
        SELECT m.gcp_service, c.gcp_region, m.gcp_sku_id, COUNT(*) AS unreachable_rows
        FROM aws_li_to_gcp_li m
        JOIN aws_li_catalog c USING (aws_li_key)
        WHERE m.gcp_sku_id IS NOT NULL
          AND m.strategy IN ('map','break_down')
          AND NOT EXISTS (
              SELECT 1 FROM gcp_sku_rates r
              WHERE r.gcp_sku_id = m.gcp_sku_id
                AND (r.region = c.gcp_region OR r.region = 'global')
          )
        GROUP BY m.gcp_service, c.gcp_region, m.gcp_sku_id
    """).fetchall()

    if unreachable:
        _write_rate_gaps(unreachable)
        for svc, region, sku_id, count in unreachable:
            conn.execute("""
                UPDATE aws_li_to_gcp_li SET strategy = 'outlier_triage',
                    projection_note = COALESCE(projection_note || ' ', '') ||
                        '[rate-fill-gap: no rate for region — routed to Phase 5]'
                WHERE gcp_sku_id = ? AND strategy IN ('map','break_down')
            """, [sku_id])
        log.info(f"Sanity check: {len(unreachable)} unreachable SKU/region pair(s) moved to outlier_triage")
    else:
        log.info("Sanity check: all mapped SKUs have reachable rates")

    log.info("Rate fill complete")

if __name__ == "__main__":
    main()

