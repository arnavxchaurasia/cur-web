#!/usr/bin/env python3
"""
merge_mappings.py — Bulk-INSERT all mechanic-group mapping temp files into
aws_li_to_gcp_li in one transaction.

Usage:
    python3 merge_mappings.py <projection.duckdb> <mappings_dir>

The mappings_dir must contain one or more <group>_mappings.json files, each
a JSON array of mapping objects matching the aws_li_to_gcp_li schema.

Exits 0 on success. Exits 1 if any expected group file is missing — the
orchestrator should retry the missing group's agent before merging.
"""

import json
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import duckdb

CATALOG_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "catalog.duckdb")

# Synthetic SKU IDs that are never in catalog.duckdb (they're injected by apply_rates.py).
# Must be whitelisted here so the phantom-SKU filter does NOT null them out — they
# carry correct deterministic rates and must not be silently replaced by word-overlap
# catalog matches that produce wrong rates (e.g. 2x egress).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from egress_rates import EGRESS_SKUS as _EGRESS_SKUS
    _SYNTHETIC_SKUS = {sku_id for sku_id, _, _ in _EGRESS_SKUS.values()}
except Exception:
    _SYNTHETIC_SKUS = set()

# Columns that can appear in a mapping JSON object.
# Order matches the INSERT statement below.
COLUMNS = [
    "aws_li_key",
    "gcp_service",
    "gcp_sku_id",
    "gcp_sku_name",
    "component",
    "strategy",
    "unit_multiplier",
    "gcp_region",
    "projection_note",
    "mapping_confidence",
    "is_workload",
    "break_down",
]


def load_mappings(mappings_dir: str) -> tuple[list[dict], list[str]]:
    """Return (all_rows, missing_groups).

    missing_groups is non-empty when the manifest lists a group but its
    *_mappings.json file does not exist yet (agent didn't finish).
    """
    # Discover which groups were dispatched via the manifest
    manifest_path = os.path.join(os.path.dirname(mappings_dir.rstrip("/")), "phase2_manifest.json")
    expected_groups: list[str] = []
    if os.path.exists(manifest_path):
        manifest = json.load(open(manifest_path))
        expected_groups = [
            g for g, meta in manifest.items()
            if not g.startswith("_") and isinstance(meta, dict) and meta.get("needs_llm", True)
        ]

    all_rows: list[dict] = []
    missing: list[str] = []

    # Load all *_mappings.json files present
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

    # Check for groups expected but absent
    found_groups = {f.replace("_mappings.json", "") for f in found_files}
    for g in expected_groups:
        if g not in found_groups:
            missing.append(g)

    return all_rows, missing


def ensure_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS aws_li_to_gcp_li (
            aws_li_key          TEXT,
            gcp_service         TEXT,
            gcp_sku_id          TEXT,
            gcp_sku_name        TEXT,
            gcp_sku_unit        TEXT,
            component           TEXT,
            strategy            TEXT,
            unit_multiplier     DOUBLE,
            gcp_region          TEXT,
            projection_note     TEXT,
            mapping_confidence  DOUBLE,
            is_workload         BOOLEAN,
            break_down          BOOLEAN
        )
    """)


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <projection.duckdb> <mappings_dir>", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    mappings_dir = sys.argv[2]

    if not os.path.exists(mappings_dir):
        print(f"ERROR: mappings dir not found: {mappings_dir}", file=sys.stderr)
        sys.exit(1)

    rows, missing = load_mappings(mappings_dir)

    if missing:
        print(f"WARNING: {len(missing)} group(s) have no mapping file — proceeding with available files:", file=sys.stderr)
        for g in missing:
            print(f"  missing: {g}_mappings.json", file=sys.stderr)

    if not rows:
        print("WARNING: no mapping rows found in any file — aws_li_to_gcp_li will be empty.")

    con = duckdb.connect(db_path)
    ensure_table(con)

    # Truncate existing mappings so re-runs are idempotent
    con.execute("DELETE FROM aws_li_to_gcp_li")

    # Validate SKU IDs against catalog — null out any that don't exist.
    # The LLM occasionally fabricates SKU IDs; phantom IDs silently produce
    # NULL projected costs that only surface in detect_outliers query E/I.
    valid_skus: set[str] = set()
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
                print(f"  INVALID SKU nulled: {sku!r} on {r.get('aws_li_key')} "
                      f"({r.get('gcp_sku_name', '')}) — not in catalog", file=sys.stderr)
                r["gcp_sku_id"] = None
                nulled += 1
        if nulled:
            print(f"  {nulled} phantom SKU ID(s) nulled — apply_rates.py will fall back to passthrough.")

    # --- Deduplication pass 1: exact (aws_li_key, component) duplicates ---
    # The LLM occasionally emits duplicate rows for the same (key, component).
    # Keep the last occurrence (most specific) to avoid multiplying costs.
    seen: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("aws_li_key"), r.get("component"))
        seen[key] = r
    rows = list(seen.values())

    # --- Deduplication pass 2: break_down + all-passthrough collapse ---
    # When the LLM self-hosts a service (OpenSearch/MSK → GCE) but sets every
    # break_down component to strategy='passthrough', each component independently
    # returns aws_amortized_cost in the projection view → N× multiplication.
    # Fix: if every row for a given aws_li_key has break_down=True and
    # strategy='passthrough', collapse them all to a single non-break_down
    # passthrough row (preserving gcp_service and the first projection_note).
    from collections import defaultdict
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_key[r.get("aws_li_key")].append(r)

    collapsed_rows: list[dict] = []
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
            print(
                f"  COLLAPSED {len(group)} break_down+passthrough rows → 1 passthrough: {li_key}",
                file=sys.stderr,
            )
        else:
            collapsed_rows.extend(group)

    if collapsed_count:
        print(
            f"  {collapsed_count} aws_li_key(s) had all-passthrough break_down — collapsed to avoid N× cost multiplication.",
            file=sys.stderr,
        )
    rows = collapsed_rows

    # Build tuples in column order
    records = []
    for r in rows:
        records.append(tuple(r.get(col) for col in COLUMNS))

    placeholders = ", ".join(["?"] * len(COLUMNS))
    col_list = ", ".join(COLUMNS)
    con.executemany(
        f"INSERT INTO aws_li_to_gcp_li ({col_list}) VALUES ({placeholders})",
        records,
    )
    con.commit()

    # Per-group summary
    stats = con.execute("""
        SELECT
            m.mechanic_group,
            COUNT(*) AS mapped_rows
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

    con.close()
    print(f"\nMerge complete. {total} rows inserted into aws_li_to_gcp_li.")
    sys.exit(0)


if __name__ == "__main__":
    main()
