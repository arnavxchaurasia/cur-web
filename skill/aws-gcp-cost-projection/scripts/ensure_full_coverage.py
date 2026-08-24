#!/usr/bin/env python3
"""
ensure_full_coverage.py <projection.duckdb>

Deterministic backstop for the mapping_coverage gate. Runs ONLY after Phase 2's
normal LLM pass + one retry have both had a chance to map every row — it is the
last resort, not a substitute for real mapping.

Why this exists
----------------
Phase 2 asks the LLM to map three groups (compute_breakdown, managed_db, misc).
On a large bill's misc group, the LLM can drop rows — token budget, an off-by-one
in its own bookkeeping, or simply missing a line in a long response. Before this
script existed, ANY row left unmapped after the retry caused the entire report
to fail with no report at all, even when 99%+ of the bill's spend was mapped
correctly. That is disproportionate: an honest passthrough for a handful of
leftover rows is a far better outcome than no report.

What it does
------------
Finds every aws_li_catalog row with no corresponding aws_li_to_gcp_li row and
inserts ONE passthrough mapping for it: AWS cost carried forward 1:1 (never an
invented GCP figure), clearly labeled "Manual Review — Unmapped" so it is
visible in the report and easy to audit, never silently blended into a
confident-looking number.

This mirrors the existing precedent in this pipeline (service_classifier.py's
"review" mode, fix_storage_misroute.py's backstop) — passthrough-and-flag is
the established honest fallback here, not a new pattern.

Idempotent. Never fatal — prints what it backstopped and exits 0.
"""
import sys
try:
    import duckdb
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"ensure_full_coverage: duckdb import failed ({e}); skipping\n")
    sys.exit(0)

NOTE = "[backstop] LLM left this row unmapped after retry — carried at AWS cost for manual review (ensure_full_coverage.py)"

# commitment_discount/negative_cost rows are NOT ordinary unmapped rows: their
# whole point is strategy='ignore' (apply_commitment_ignores.py's own docstring:
# "amortized costs already reflected in effective rates — mapping would
# double-count"). If apply_commitment_ignores.py ever misses one (e.g. a job
# resumed from a checkpoint captured before it ran), backstopping it as a normal
# passthrough would carry its full AWS cost onto the GCP side too — inflating
# the GCP total for a cost that's already baked into other rows' rates, and
# desyncing from render_report.py's AWS-side total (which deliberately excludes
# commitment_discount to avoid the same double-count). Ignore, not passthrough.
_IGNORE_GROUPS = ("commitment_discount", "negative_cost")


def main():
    if len(sys.argv) < 2:
        sys.exit(0)
    db = sys.argv[1]
    con = duckdb.connect(db)

    rows = con.execute("""
        SELECT c.aws_li_key, c.gcp_region, c.is_workload, c.aws_amortized_cost,
               c.product, c.mechanic_group
        FROM aws_li_catalog c
        WHERE NOT EXISTS (SELECT 1 FROM aws_li_to_gcp_li m WHERE m.aws_li_key = c.aws_li_key)
    """).fetchall()

    if not rows:
        print("ensure_full_coverage: no gaps found, nothing to do")
        sys.exit(0)

    records = [
        (
            aws_li_key,
            "Non-workload / Commitment (ignored)" if mechanic_group in _IGNORE_GROUPS else "Manual Review — Unmapped",
            None, None, None,
            "ignore" if mechanic_group in _IGNORE_GROUPS else "passthrough",
            1.0, gcp_region, NOTE, 0.10, bool(is_workload), False,
        )
        for (aws_li_key, gcp_region, is_workload, _cost, _product, mechanic_group) in rows
    ]
    con.executemany(
        """
        INSERT INTO aws_li_to_gcp_li
            (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, gcp_sku_unit,
             strategy, unit_multiplier, gcp_region, projection_note,
             mapping_confidence, is_workload, break_down)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        records,
    )
    con.commit()

    total_cost = sum(r[3] or 0 for r in rows)
    ignored_n = sum(1 for r in rows if r[5] in _IGNORE_GROUPS)
    print(f"ensure_full_coverage: backstopped {len(rows)} row(s) totaling "
          f"${total_cost:,.2f} ({ignored_n} as ignore/commitment, "
          f"{len(rows) - ignored_n} as passthrough/Manual Review) — loud, not silent:")
    for aws_li_key, _region, _wl, cost, product, mechanic_group in sorted(rows, key=lambda r: -(r[3] or 0))[:20]:
        tag = "ignore" if mechanic_group in _IGNORE_GROUPS else "passthrough"
        print(f"  {aws_li_key[:12]}  ${cost or 0:>10,.2f}  [{tag}]  {product}")


if __name__ == "__main__":
    main()
