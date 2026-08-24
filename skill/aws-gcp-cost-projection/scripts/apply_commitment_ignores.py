#!/usr/bin/env python3
"""
apply_commitment_ignores.py — Write ignore mappings for commitment_discount and negative_cost rows.

commitment_discount: RIFee, SavingsPlanRecurringFee, EdpDiscount — amortized costs
already reflected in effective rates. Mapping would double-count.

negative_cost: credits, refunds, RI/SP negations — AWS-specific billing mechanics
with no GCP equivalent. Ignored so they don't consume LLM tokens.

Usage:
    python3 apply_commitment_ignores.py <projection.duckdb>

Writes: projection-audit/mappings/commitment_discount_mappings.json
        projection-audit/mappings/negative_cost_mappings.json
"""

import json, os, sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import duckdb


def _safe_path(base: str, *parts: str) -> str:
    """Resolve path and verify it stays within base (path traversal guard)."""
    p = os.path.realpath(os.path.join(base, *parts))
    if not p.startswith(os.path.realpath(base) + os.sep) and p != os.path.realpath(base):
        raise ValueError(f"Path escapes base directory: {p}")
    return p


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <projection.duckdb>", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    con = duckdb.connect(db_path)

    commitment_rows = con.execute("""
        SELECT aws_li_key
        FROM aws_li_catalog
        WHERE mechanic_group = 'commitment_discount'
    """).fetchall()

    negative_rows = con.execute("""
        SELECT aws_li_key, aws_amortized_cost, line_item_type
        FROM aws_li_catalog
        WHERE mechanic_group = 'negative_cost'
    """).fetchall()

    con.close()

    out_dir = _safe_path(os.path.dirname(db_path), "mappings")
    os.makedirs(out_dir, exist_ok=True)

    commitment_mappings = [
        {"aws_li_key": r[0], "strategy": "ignore", "mapping_confidence": 1.0,
         "projection_note": "commitment_discount: amortized RI/SP/EDP cost, already in effective rates"}
        for r in commitment_rows
    ]
    out_path = _safe_path(out_dir, "commitment_discount_mappings.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(commitment_mappings, f, indent=2)
    print(f"Wrote {len(commitment_mappings)} commitment_discount ignore rows → {out_path}")

    negative_mappings = [
        {"aws_li_key": r[0], "strategy": "ignore", "mapping_confidence": 1.0,
         "projection_note": f"negative_cost: credit/refund/negation (${r[1]:.2f}, {r[2] or 'unknown type'}) — no GCP equivalent"}
        for r in negative_rows
    ]
    out_path = _safe_path(out_dir, "negative_cost_mappings.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(negative_mappings, f, indent=2)
    print(f"Wrote {len(negative_mappings)} negative_cost ignore rows → {out_path}")


if __name__ == "__main__":
    main()
