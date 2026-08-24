#!/usr/bin/env python3
"""
job_inspect.py — Single-command post-run debugger.

Shows: mechanic group breakdown, mapping coverage, confidence distribution,
validator violations, which temp files exist, SKU gaps, and outlier rows.

Usage:
    python3 job_inspect.py <job_dir>

job_dir is the projection-audit directory (contains projection.duckdb,
phase2_manifest.json, mappings/, etc.)
"""

import json, os, sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import duckdb


def section(title):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <job_dir>")
        sys.exit(1)

    job_dir = sys.argv[1].rstrip("/")
    db_path = os.path.join(job_dir, "projection.duckdb")
    manifest_path = os.path.join(job_dir, "phase2_manifest.json")
    mappings_dir = os.path.join(job_dir, "mappings")

    if not os.path.exists(db_path):
        print(f"ERROR: {db_path} not found"); sys.exit(1)

    con = duckdb.connect(db_path, read_only=True)
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}

    # ── 1. Mechanic group breakdown ───────────────────────────────────────
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

    # ── 2. Temp file status ───────────────────────────────────────────────
    section("Phase 2 Temp Files")
    if os.path.exists(manifest_path):
        manifest = json.load(open(manifest_path))
        for group, meta in sorted(manifest.items()):
            fpath = os.path.join(mappings_dir, f"{group}_mappings.json")
            exists = os.path.exists(fpath)
            row_count = len(json.load(open(fpath))) if exists else 0
            status = f"✓ {row_count} rows" if exists else "✗ MISSING"
            llm = "script" if not meta.get("needs_llm") else "LLM"
            print(f"  {group:<28} [{llm:<6}]  {status}")
    else:
        print("  phase2_manifest.json not found — Phase 1 may not have completed")

    # ── 3. Mapping coverage ───────────────────────────────────────────────
    if "aws_li_to_gcp_li" in tables and "aws_li_catalog" in tables:
        section("Mapping Coverage")
        cov = con.execute("""
            SELECT
                c.mechanic_group,
                COUNT(DISTINCT c.aws_li_key)                       AS catalog_rows,
                COUNT(DISTINCT m.aws_li_key)                       AS mapped_rows,
                COALESCE(AVG(m.mapping_confidence), 0)             AS avg_conf,
                COUNT(CASE WHEN m.strategy = 'map'          THEN 1 END) AS mapped,
                COUNT(CASE WHEN m.strategy = 'ignore'       THEN 1 END) AS ignored,
                COUNT(CASE WHEN m.strategy = 'passthrough'  THEN 1 END) AS passthrough,
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

    # ── 4. Confidence distribution ────────────────────────────────────────
    if "aws_li_to_gcp_li" in tables:
        section("Confidence Distribution")
        dist = con.execute("""
            SELECT
                CASE
                    WHEN mapping_confidence >= 0.9  THEN 'high   (≥0.90)'
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

    # ── 5. Validator violations ───────────────────────────────────────────
    violations_path = os.path.join(job_dir, "validator_violations.json")
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
                    print(f"    · {item.get('message', item)}")
                if len(items) > 3:
                    print(f"    · ... +{len(items)-3} more")
        else:
            print(f"  {v}")
    else:
        print("  No violations file found (validator not yet run)")

    # ── 6. SKU gaps (mapped rows with no rate) ────────────────────────────
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

    # ── 7. Outlier rows ───────────────────────────────────────────────────
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

    # ── 8. GCP projection total ───────────────────────────────────────────
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
            print(f"  Ratio GCP/AWS:  {total/aws_total:.3f}x" if total else "")

    con.close()


if __name__ == "__main__":
    main()
