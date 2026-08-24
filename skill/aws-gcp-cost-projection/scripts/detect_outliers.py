#!/usr/bin/env python3
"""
detect_outliers.py — Phase 5 pre-LLM detection pass.

Runs all outlier queries, splits results into:
  structural_outliers.md   (D, E, G, B, C, H, I — deterministic root causes)
  pricing_outliers.md      (A1, A2, F — ratio anomalies)
  outliers_data.json       (machine-readable raw results for auto_triage.py)

Early gate: if total flagged rows > outlier_pattern_summary_threshold (review-config.json),
writes outlier_pattern_summary.md with a product/service breakdown and continues.
Never exits with code 1 — report generation must always complete.
"""
import duckdb
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from projection_view import create_projection_view
from config_loader import load_data_config as _cfg

JOB_DIR = os.getcwd()
DB_PATH = os.path.join(JOB_DIR, "projection-audit", "projection.duckdb")
STRUCTURAL_FILE = os.path.join(JOB_DIR, "structural_outliers.md")
PRICING_FILE    = os.path.join(JOB_DIR, "pricing_outliers.md")
DATA_FILE       = os.path.join(JOB_DIR, "outliers_data.json")

# Keep backward-compat file so any external tooling that reads outliers.md still works.
LEGACY_FILE = os.path.join(JOB_DIR, "outliers.md")

# ── Load all thresholds from review-config.json ───────────────────────────── #
_rc = _cfg("review-config")

# Pattern summary: diagnostic, never a gate — see comment in the threshold block below.
PATTERN_SUMMARY_THRESHOLD = _rc.get("outlier_pattern_summary_threshold", 20)
_TOP_N_STDERR = _rc.get("outlier_pattern_summary_top_n_stderr", 5)
_TOP_N_FILE   = _rc.get("outlier_pattern_summary_top_n_file", 10)

# GCP services that have CUD/committed-use pricing (used in A1, E, H queries).
_CUD_SERVICES: list[str] = _rc.get("outlier_cud_services", [
    "Compute Engine", "Cloud SQL", "AlloyDB",
    "Cloud Memorystore", "Cloud Memorystore for Redis", "Cloud Memorystore for Memcached",
])
_CUD_SERVICES_SQL = ", ".join(f"'{s}'" for s in _CUD_SERVICES)

# Query thresholds
_A1_FLOOR      = _rc.get("outlier_a1_cost_floor_usd", 50)
_A1_RATIO_CMS  = _rc.get("outlier_a1_ratio_compute_sql_memorystore", 1.3)
_A1_RATIO_OTH  = _rc.get("outlier_a1_ratio_other", 1.5)

_A2_FLOOR      = _rc.get("outlier_a2_cost_floor_usd", 1)
_A2_UPPER      = _rc.get("outlier_a2_ratio_upper", 10)
_A2_LOWER      = _rc.get("outlier_a2_ratio_lower", 0.05)

_B_AWS_MAX     = _rc.get("outlier_b_aws_cost_max_usd", 1)
_B_GCP_MIN     = _rc.get("outlier_b_gcp_cost_min_usd", 10)

_C_FLOOR       = _rc.get("outlier_c_cost_floor_usd", 50)
_F_FLOOR       = _rc.get("outlier_f_cost_floor_usd", 20)
_F_RATIO       = _rc.get("outlier_f_ratio", 2.0)
_H_FLOOR       = _rc.get("outlier_h_cost_floor_usd", 10)
_I_FLOOR       = _rc.get("outlier_i_cost_floor_usd", 1)

_G_CORE_DELTA  = _rc.get("outlier_g_core_delta_tolerance", 0.5)
_G_RAM_DELTA   = _rc.get("outlier_g_ram_delta_tolerance", 1.0)
# ─────────────────────────────────────────────────────────────────────────── #


def run_query(conn, query):
    """Return (cols, rows) or (None, None) on error."""
    try:
        rows = conn.execute(query).fetchall()
        cols = [d[0] for d in conn.description]
        return cols, rows
    except Exception as e:
        print(f"  Query error: {e}", file=sys.stderr)
        return None, None


def write_section(f, header, description, cols, rows):
    f.write(f"### {header}\n")
    f.write(f"{description}\n\n")
    if rows is None:
        f.write("**Status: ERROR (query failed)**\n\n")
        return
    if not rows:
        f.write("**Status: PASS (0 rows found)**\n\n")
        return
    f.write(f"**Status: FAIL ({len(rows)} rows found)**\n\n")
    f.write("| " + " | ".join(cols) + " |\n")
    f.write("|" + "|".join(["---"] * len(cols)) + "|\n")
    for r in rows:
        f.write("| " + " | ".join([str(x) if x is not None else "" for x in r]) + " |\n")
    f.write("\n")


def rows_to_dicts(cols, rows):
    if cols is None or rows is None:
        return []
    return [dict(zip(cols, r)) for r in rows]


def main():
    if not os.path.exists(DB_PATH):
        print("Database not found.")
        sys.exit(0)

    conn = duckdb.connect(DB_PATH)
    create_projection_view(conn)

    # ------------------------------------------------------------------ #
    # Run all queries                                                       #
    # ------------------------------------------------------------------ #

    queries = {}

    # A1: Over-projection (pricing anomaly)
    queries["A1"] = run_query(conn, f"""
        SELECT p.aws_li_key, p.product, p.gcp_service, p.gcp_sku_id,
               m.gcp_sku_name, m.unit_multiplier, m.component,
               ROUND(p.aws_amortized_cost,2) AS aws,
               ROUND(p.gcp_projected_cost,2) AS gcp,
               ROUND(p.gcp_projected_cost / NULLIF(p.aws_amortized_cost,0), 2) AS ratio
        FROM   gcp_projection p
        JOIN   aws_li_to_gcp_li m
          ON   m.aws_li_key = p.aws_li_key AND m.component IS NOT DISTINCT FROM p.component
        WHERE  p.strategy NOT IN ('ignore','passthrough')
          AND  p.aws_amortized_cost > {_A1_FLOOR}
          AND  p.product NOT ILIKE '%Data Transfer%'
          AND  (
            ( p.gcp_service IN ({_CUD_SERVICES_SQL})
              AND p.gcp_projected_cost > p.aws_amortized_cost * {_A1_RATIO_CMS}
            )
            OR
            ( p.gcp_service NOT IN ({_CUD_SERVICES_SQL})
              AND p.gcp_projected_cost > p.aws_amortized_cost * {_A1_RATIO_OTH}
            )
          )
    """)

    # A2: Extreme ratio outliers (pricing anomaly)
    queries["A2"] = run_query(conn, f"""
        SELECT p.aws_li_key, p.product, p.gcp_service, p.gcp_sku_id,
               m.gcp_sku_name, m.unit_multiplier, m.component,
               ROUND(p.aws_amortized_cost,2) AS aws,
               ROUND(p.gcp_projected_cost,2) AS gcp,
               ROUND(p.gcp_projected_cost / NULLIF(p.aws_amortized_cost,0), 2) AS ratio
        FROM   gcp_projection p
        JOIN   aws_li_to_gcp_li m
          ON   m.aws_li_key = p.aws_li_key AND m.component IS NOT DISTINCT FROM p.component
        WHERE  p.strategy NOT IN ('ignore','passthrough')
          AND  p.aws_amortized_cost > {_A2_FLOOR}
          AND  ( p.gcp_projected_cost > p.aws_amortized_cost * {_A2_UPPER}
              OR p.gcp_projected_cost < p.aws_amortized_cost * {_A2_LOWER} )
    """)

    # B: Phantom GCP cost (structural)
    # component included: a break_down row shares one aws_li_key across core/ram/
    # storage — without it, auto_triage.py can't tell WHICH component is actually
    # phantom-priced, and its set_multiplier=0 "fix" (applied without a component
    # filter) would zero out every sibling component's legitimate price too.
    queries["B"] = run_query(conn, f"""
        SELECT aws_li_key, component, product, gcp_service, gcp_sku_id,
               total_usage, ROUND(gcp_projected_cost,2) AS gcp, projection_note
        FROM   gcp_projection
        WHERE  aws_amortized_cost <= {_B_AWS_MAX}
          AND  gcp_projected_cost > {_B_GCP_MIN}
    """)

    # C: Zero rate on billable row (structural)
    queries["C"] = run_query(conn, f"""
        SELECT m.aws_li_key, m.component, m.gcp_service, m.gcp_sku_id, m.gcp_sku_name,
               ROUND(c.aws_amortized_cost,2) AS aws_cost, m.projection_note
        FROM   aws_li_to_gcp_li m
        JOIN   aws_li_catalog c USING (aws_li_key)
        JOIN   gcp_sku_rates  r ON r.gcp_sku_id = m.gcp_sku_id AND r.pricing_type = 'OnDemand'
        WHERE  m.strategy IN ('map','break_down')
          AND  r.rate_usd = 0
          AND  c.aws_amortized_cost > {_C_FLOOR}
    """)

    # D: Cross-service mismatch (structural)
    queries["D"] = run_query(conn, """
        SELECT m.aws_li_key, m.component, m.gcp_service AS mapping_says, r.gcp_service AS sku_actually,
               m.gcp_sku_id, m.projection_note
        FROM   gcp_projection p
        JOIN   aws_li_to_gcp_li m
          ON   m.aws_li_key = p.aws_li_key AND m.component IS NOT DISTINCT FROM p.component
        JOIN   gcp_sku_rates    r ON r.gcp_sku_id = m.gcp_sku_id
        WHERE  m.strategy IN ('map','break_down')
          AND  m.gcp_service IS NOT NULL AND r.gcp_service IS NOT NULL
          AND  m.gcp_service != r.gcp_service
          -- Only flag if it caused a real impact: projected cost is NULL despite being mapped.
          -- Name-only mismatches (e.g. "Cloud KMS" vs "Cloud Key Management Service (KMS)")
          -- are cosmetic — the rate lookup is keyed by SKU ID, not service name.
          -- apply_rates.py normalizes gcp_service for new runs so these disappear going forward.
          AND  p.gcp_projected_cost IS NULL
    """)

    # E: Missing CUD alias (structural)
    queries["E"] = run_query(conn, f"""
        SELECT m.aws_li_key, m.component, m.gcp_service, m.gcp_sku_id, m.projection_note
        FROM   aws_li_to_gcp_li m
        WHERE  m.strategy IN ('map','break_down')
          AND  m.gcp_service IN ({_CUD_SERVICES_SQL})
          AND  m.component IN ('core','ram')
          AND  m.projection_note NOT LIKE '%no-rate-fallback%'
          AND  m.gcp_sku_name NOT ILIKE '%preemptible%'
          AND  m.gcp_sku_name NOT ILIKE '%spot%'
          AND  NOT EXISTS (SELECT 1 FROM gcp_sku_rates r
                           WHERE r.gcp_sku_id = m.gcp_sku_id AND r.pricing_type = 'Commit1Yr')
    """)

    # F: Unit-multiplier over-projection (pricing anomaly)
    queries["F"] = run_query(conn, f"""
        SELECT p.aws_li_key, p.product, p.gcp_service, p.gcp_sku_id,
               m.gcp_sku_name, m.unit_multiplier, m.component,
               ROUND(p.aws_amortized_cost,2) AS aws,
               ROUND(p.gcp_projected_cost,2) AS gcp_od,
               ROUND(p.gcp_projected_cost / NULLIF(p.aws_amortized_cost,0), 2) AS ratio,
               m.projection_note
        FROM   gcp_projection p
        JOIN   aws_li_to_gcp_li m
          ON   m.aws_li_key = p.aws_li_key AND m.component IS NOT DISTINCT FROM p.component
        WHERE  p.strategy = 'map'
          AND  p.pricing_model = 'OnDemand'
          AND  p.line_item_type IN ('Usage')
          AND  p.aws_amortized_cost > {_F_FLOOR}
          AND  p.gcp_projected_cost > p.aws_amortized_cost * {_F_RATIO}
    """)

    # G: break_down multiplier mismatch (structural)
    queries["G"] = run_query(conn, f"""
        SELECT m.aws_li_key, c.operation, m.component,
               m.unit_multiplier AS mapped, m.projection_note,
               CASE m.component
                 WHEN 'core' THEN c.instance_vcpus
                 WHEN 'ram'  THEN c.instance_ram_gb
               END AS spec_value,
               ABS(m.unit_multiplier - CASE m.component
                 WHEN 'core' THEN c.instance_vcpus
                 WHEN 'ram'  THEN c.instance_ram_gb
               END) AS delta,
               ROUND(c.aws_amortized_cost, 2) AS aws_cost
        FROM   aws_li_to_gcp_li m
        JOIN   aws_li_catalog   c USING (aws_li_key)
        WHERE  m.strategy = 'break_down'
          AND  m.component IN ('core','ram')
          AND  c.instance_vcpus IS NOT NULL
          AND  CASE m.component
                 WHEN 'core' THEN ABS(m.unit_multiplier - c.instance_vcpus) > {_G_CORE_DELTA}
                 WHEN 'ram'  THEN ABS(m.unit_multiplier - c.instance_ram_gb) > {_G_RAM_DELTA}
               END
        ORDER BY delta DESC
    """)

    # H: RI/CUD parity check (structural). Spot/Preemptible rows are excluded —
    # projection_view.py deliberately sets gcp_cost_1yr_cud = gcp_projected_cost
    # for Spot rows (GCP Preemptible VMs can't be combined with CUDs), so that
    # equality is by design there, not a rate-loading bug. Without this
    # exclusion every correctly-priced Spot row false-positives here.
    queries["H"] = run_query(conn, f"""
        SELECT m.aws_li_key, m.component, c.product, m.gcp_service, m.gcp_sku_id,
               ROUND(p.gcp_projected_cost, 2)  AS gcp_od,
               ROUND(p.gcp_cost_1yr_cud, 2)    AS gcp_1yr,
               ROUND(p.gcp_cost_3yr_cud, 2)    AS gcp_3yr,
               m.projection_note
        FROM   gcp_projection p
        JOIN   aws_li_to_gcp_li m
          ON   m.aws_li_key = p.aws_li_key AND m.component IS NOT DISTINCT FROM p.component
        JOIN   aws_li_catalog   c ON c.aws_li_key = p.aws_li_key
        WHERE  m.strategy IN ('map','break_down')
          AND  m.component IN ('core','ram')
          AND  m.gcp_service IN ({_CUD_SERVICES_SQL})
          AND  c.pricing_model IS DISTINCT FROM 'Spot'
          AND  p.gcp_cost_1yr_cud = p.gcp_projected_cost
          AND  c.aws_amortized_cost > {_H_FLOOR}
    """)

    # I: NULL projection (structural)
    queries["I"] = run_query(conn, f"""
        SELECT m.aws_li_key, m.component, c.product, c.gcp_region, m.gcp_service, m.gcp_sku_id,
               ROUND(c.aws_amortized_cost, 2) AS aws_cost, m.projection_note
        FROM   gcp_projection p
        JOIN   aws_li_to_gcp_li m
          ON   m.aws_li_key = p.aws_li_key AND m.component IS NOT DISTINCT FROM p.component
        JOIN   aws_li_catalog   c ON c.aws_li_key = p.aws_li_key
        WHERE  m.strategy IN ('map','break_down')
          AND  p.gcp_projected_cost IS NULL
          AND  c.aws_amortized_cost > {_I_FLOOR}
          AND  m.projection_note NOT LIKE '%no-rate-fallback%'
        ORDER BY c.aws_amortized_cost DESC
    """)

    conn.close()

    # ------------------------------------------------------------------ #
    # Count rows and early gate                                            #
    # ------------------------------------------------------------------ #

    STRUCTURAL_QUERIES = ["B", "C", "D", "E", "G", "H", "I"]
    PRICING_QUERIES    = ["A1", "A2", "F"]

    def row_count(qid):
        _, rows = queries[qid]
        return len(rows) if rows else 0

    total_structural = sum(row_count(q) for q in STRUCTURAL_QUERIES)
    total_pricing    = sum(row_count(q) for q in PRICING_QUERIES)
    total            = total_structural + total_pricing

    # Count by service/product for pattern summary (helps diagnose systematic bugs).
    # This is a diagnostic threshold, not a gate — exceeding it never fails this
    # script or blocks report generation.
    if total > PATTERN_SUMMARY_THRESHOLD:
        product_counts = defaultdict(int)
        service_counts = defaultdict(int)
        for qid in STRUCTURAL_QUERIES + PRICING_QUERIES:
            cols, rows = queries[qid]
            if not rows:
                continue
            for r in rows:
                row_dict = dict(zip(cols, r))
                prod = row_dict.get("product", "")
                svc  = row_dict.get("gcp_service", "")
                if prod:
                    product_counts[prod] += 1
                if svc:
                    service_counts[svc] += 1

        print(f"outlier_gate: {total} outlier rows found (pattern-summary threshold: "
              f"{PATTERN_SUMMARY_THRESHOLD}) — non-blocking, report generation proceeds.",
              file=sys.stderr)
        print("Volume this high on a large/complex bill can be ordinary per-row variance; "
              "see outlier_pattern_summary.md if a systemic mapper issue is suspected.",
              file=sys.stderr)
        print("\nTop AWS products with outliers:", file=sys.stderr)
        for prod, cnt in sorted(product_counts.items(), key=lambda x: -x[1])[:_TOP_N_STDERR]:
            print(f"  {cnt:3d}x  {prod}", file=sys.stderr)
        print("\nTop GCP services with outliers:", file=sys.stderr)
        for svc, cnt in sorted(service_counts.items(), key=lambda x: -x[1])[:_TOP_N_STDERR]:
            print(f"  {cnt:3d}x  {svc}", file=sys.stderr)
        print(f"\nBreakdown: structural={total_structural} pricing={total_pricing}", file=sys.stderr)

        # Write a summary file for the user to inspect, then continue.
        # Do NOT exit(1) — report generation must always proceed.
        with open(os.path.join(JOB_DIR, "outlier_pattern_summary.md"), "w", encoding="utf-8") as f:
            f.write(f"# Outlier Pattern Summary\n\n")
            f.write(f"**Non-blocking**: {total} outlier rows found "
                    f"(pattern-summary threshold: {PATTERN_SUMMARY_THRESHOLD}). "
                    f"This does not fail the job or affect report generation.\n\n")
            f.write("On a large or service-diverse bill this volume can be ordinary "
                    "per-row variance rather than a systemic issue — review the "
                    "product/service breakdown below before assuming a mapper bug.\n\n")
            f.write("## Top AWS Products\n\n")
            for prod, cnt in sorted(product_counts.items(), key=lambda x: -x[1])[:_TOP_N_FILE]:
                f.write(f"- {cnt}x `{prod}`\n")
            f.write("\n## Top GCP Services\n\n")
            for svc, cnt in sorted(service_counts.items(), key=lambda x: -x[1])[:_TOP_N_FILE]:
                f.write(f"- {cnt}x `{svc}`\n")
            f.write(f"\n## Row Counts by Query\n\n")
            for qid in STRUCTURAL_QUERIES + PRICING_QUERIES:
                f.write(f"- {qid}: {row_count(qid)}\n")

    # ------------------------------------------------------------------ #
    # Write structural_outliers.md                                         #
    # ------------------------------------------------------------------ #

    with open(STRUCTURAL_FILE, "w", encoding="utf-8") as f:
        f.write("# Structural Outliers\n\n")
        f.write("Deterministic root causes: wrong service label, missing CUD alias, "
                "multiplier mismatch, phantom cost, zero rate, NULL projection.\n")
        f.write(f"Total: {total_structural} rows\n\n")

        write_section(f, "B. Phantom GCP cost",
            "AWS ~$0 but GCP cost is large — always a unit_multiplier bug.",
            *queries["B"])
        write_section(f, "C. Zero rate on billable row",
            "SKU resolved but rate_usd=0 and the AWS row carries non-trivial cost.",
            *queries["C"])
        write_section(f, "D. Cross-service mismatch",
            "Mapping declares service X but the resolved SKU belongs to service Y in the catalog.",
            *queries["D"])
        write_section(f, "E. Missing CUD alias",
            "Compute Engine / Cloud SQL / Memorystore rows missing Commit1Yr pricing row.",
            *queries["E"])
        write_section(f, "G. break_down multiplier mismatch",
            "Inferred unit_multiplier doesn't match instance spec (vcpus or ram_gb).",
            *queries["G"])
        write_section(f, "H. RI/CUD parity (missing CUD rate row)",
            "Committed rows where 1yr CUD equals OD rate — CUD rate row absent.",
            *queries["H"])
        write_section(f, "I. NULL projection",
            "Mapped rows where gcp_projected_cost IS NULL (rate-fill missed this SKU or region).",
            *queries["I"])

    # ------------------------------------------------------------------ #
    # Write pricing_outliers.md                                            #
    # ------------------------------------------------------------------ #

    with open(PRICING_FILE, "w", encoding="utf-8") as f:
        f.write("# Pricing Outliers\n\n")
        f.write("Ratio anomalies visible only after rates are applied. "
                "Root causes: wrong SKU tier, wrong unit_multiplier, wrong rate lookup.\n")
        f.write(f"Total: {total_pricing} rows\n\n")

        write_section(f, "A1. Over-projection (GCP costs more than AWS)",
            "GCP projected cost materially higher than AWS — likely wrong SKU or unit_multiplier.",
            *queries["A1"])
        write_section(f, "A2. Extreme ratio outliers (>10x over or <5% of AWS)",
            "Ratio >10x → wrong SKU. Ratio <0.05x → unit_multiplier bug.",
            *queries["A2"])
        write_section(f, "F. Unit-multiplier over-projection",
            "GCP OD cost >2x AWS for a mapped row — likely wrong unit_multiplier or SKU tier.",
            *queries["F"])

    # ------------------------------------------------------------------ #
    # Write legacy outliers.md (backward compat)                           #
    # ------------------------------------------------------------------ #

    with open(LEGACY_FILE, "w", encoding="utf-8") as f:
        f.write("# Outlier Triage Report\n\n")
        f.write(f"Structural outliers: {total_structural}  |  Pricing outliers: {total_pricing}\n\n")
        f.write("See `structural_outliers.md` and `pricing_outliers.md` for details.\n")
        f.write("See `triage_suggestions.md` (written by auto_triage.py) for LLM input.\n")

    # ------------------------------------------------------------------ #
    # Write outliers_data.json (machine-readable for auto_triage.py)       #
    # ------------------------------------------------------------------ #

    data = {
        "total": total,
        "total_structural": total_structural,
        "total_pricing": total_pricing,
    }
    for qid in STRUCTURAL_QUERIES + PRICING_QUERIES:
        cols, rows = queries[qid]
        data[qid] = rows_to_dicts(cols, rows)

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"detect_outliers: structural={total_structural} pricing={total_pricing} total={total}")

if __name__ == "__main__":
    main()
