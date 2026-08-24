#!/usr/bin/env python3
"""
calibrate_confidence.py — Service-specific confidence ceilings, post-merge.

Applies deterministic caps to aws_li_to_gcp_li after the LLM mapping
completes. High-ambiguity services should never carry the same confidence
as a clean EC2→Compute Engine mapping — CUR alone cannot reveal their
full deployment topology.

Caps applied:
  OpenSearch compute → 0.70  (architecture ambiguity: GCE vs managed service)
  MSK / Kafka         → 0.70  (VM-only model ignores replication topology)
  RDS / Aurora        → 0.72  (HA intent, latency, connection limits not in CUR)
  ElastiCache         → 0.72  (cluster topology not in CUR)
  Windows instances   → 0.75  (license premium not modeled in GCP pricing)

Runs as Phase 2 post_llm_script, after merge_mappings.py.
"""

import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import os
import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import load_data_config as _cfg


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


def main():
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

        # `>=` (not `>`): a row whose upstream mapper already set confidence to
        # exactly the ceiling (e.g. OpenSearch's own 0.70 cap) still needs this
        # rule's disclosure note — `>` silently skipped it since the row was
        # never ABOVE the ceiling, only AT it, even though the note is what a
        # customer actually needs to see. Confirmed real: an OpenSearch compute
        # row landed at mapping_confidence=0.70 with no "architecture review
        # recommended" text at all.
        not_already_noted = ""
        if note:
            marker = note[:30].replace("'", "''")
            not_already_noted = f" AND (m.projection_note IS NULL OR m.projection_note NOT ILIKE '%{marker}%')"

        # Count rows needing the cap/note before updating
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

    # Windows: cap + license note (keyed on aws_li_catalog.operating_system or existing note)
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


if __name__ == "__main__":
    main()
