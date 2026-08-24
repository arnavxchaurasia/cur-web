#!/usr/bin/env python3
"""
outlier_gate.py <projection.duckdb>

Deterministic safety net: clamp any implausible cost blowup rows to passthrough
(GCP = AWS) so the report always generates. Catches the class of bug where a
single mis-mapped SKU (e.g. CloudTrail's 790,600 events wrongly mapped to Cloud
Storage) inflates the total — Bill3 shipped $2.77 -> $38,247 for exactly this
reason.

Rules (a row must have real cost to count):
  R1 single-row ratio : gcp > 50x aws  AND gcp > $100
  R2 single-row abs   : gcp > $10,000  AND aws < $100
  R3 wrong-service    : gcp_service = 'Cloud Storage' but the AWS product is NOT
                        a storage service (S3/Glacier). Non-storage services must
                        never fall back to Cloud Storage.
  R4 whole-bill       : SUM(gcp) > 2 x SUM(aws)

On any violation: write outlier_violations.md (rows named), clamp the bad rows
to AWS-cost passthrough, and exit 0 so the orchestrator continues to report
generation. The report will show clamped rows marked as passthrough. Never
exit 1 — a report with clamped rows is always better than no report.
"""
import os, sys
try:
    import duckdb
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"outlier_gate: duckdb import failed ({e}); skipping\n")
    sys.exit(0)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import load_data_config as _cfg

_rc = _cfg("review-config")

# Thresholds — all loaded from data/review-config.json. Change values there.
_R1_AWS_FLOOR  = _rc.get("outlier_gate_r1_aws_floor_usd", 1.0)
_R1_RATIO      = _rc.get("outlier_gate_r1_ratio",         50)
_R1_GCP_FLOOR  = _rc.get("outlier_gate_r1_gcp_floor_usd", 25.0)
_R2_GCP_ABS    = _rc.get("outlier_gate_r2_gcp_abs_usd",   10000.0)
_R2_AWS_MAX    = _rc.get("outlier_gate_r2_aws_max_usd",   100.0)
_R3_INFLATION  = _rc.get("outlier_gate_r3_inflation_ratio", 1.5)
_R4_RATIO      = _rc.get("outlier_gate_r4_whole_bill_ratio", 2.0)
_R5_AWS_FLOOR  = _rc.get("outlier_gate_r5_aws_floor_usd", 50.0)
_R5_GCP_RATIO  = _rc.get("outlier_gate_r5_gcp_ratio",     0.10)

# AWS products that legitimately map to Cloud Storage (substring match, case-insens).
# NB: do NOT use bare "s3" — it false-matches region codes like "APS3" in a
# product name (e.g. "AWS CloudTrail APS3-InsightsEvents").
# List loaded from data/review-config.json → outlier_gate_storage_ok_products.
_STORAGE_OK = tuple(_rc.get("outlier_gate_storage_ok_products",
                             ["simple storage service", "glacier", "storage gateway"]))


def main():
    if len(sys.argv) < 2:
        sys.exit(0)
    db = sys.argv[1]
    con = duckdb.connect(db)

    # gcp_projection already carries one row per component (core/ram/storage/...)
    # for a break_down instance. Joining aws_li_to_gcp_li back in via bare
    # USING (aws_li_key) — with no component match — creates a cartesian product
    # for any multi-component row (N components in p x N in m = N^2 result rows),
    # which both double-counts aws_amortized_cost in tot_aws below AND lets the
    # clamp UPDATE further down catch rows it never actually evaluated. Matching
    # on component keeps this a clean one-to-one join, one result row per
    # physical (aws_li_key, component).
    rows = con.execute(
        """
        SELECT c.product, m.gcp_service, m.gcp_sku_name,
               p.aws_amortized_cost AS aws, p.gcp_projected_cost AS gcp,
               p.aws_li_key, p.component
        FROM gcp_projection p
        JOIN aws_li_catalog c   USING (aws_li_key)
        JOIN aws_li_to_gcp_li m
          ON m.aws_li_key = p.aws_li_key AND m.component IS NOT DISTINCT FROM p.component
        WHERE p.gcp_projected_cost IS NOT NULL
        """
    ).fetchall()

    # tot_aws must count each AWS line item's cost ONCE, not once per component —
    # a break_down row's aws_amortized_cost is the same physical charge repeated
    # across every component row. Sum from aws_li_catalog directly (one row per
    # line item) rather than from the per-component `rows` above, matching the
    # same pattern render_report.py already uses for its totals.
    tot_aws = con.execute(
        "SELECT COALESCE(SUM(aws_amortized_cost), 0) FROM aws_li_catalog "
        "WHERE aws_li_key IN (SELECT DISTINCT aws_li_key FROM gcp_projection WHERE gcp_projected_cost IS NOT NULL)"
    ).fetchone()[0]
    tot_gcp = sum((r[4] or 0) for r in rows)

    # R5 needs the FULL line item's projected cost (summed across every
    # component), not any single component in isolation — unlike R1/R2/R3,
    # which check "is THIS component's own rate implausible" (correctly
    # per-component, since a license fee or an accelerator charge being
    # individually tiny relative to the whole item's AWS cost is completely
    # normal and expected), R5 asks "is the WHOLE item suspiciously cheap."
    # Confirmed real false-positive: a g5.2xlarge row (accelerator+core+ram,
    # AWS $899.30) was flagged as "$899.30 -> $69.28" purely because the RAM
    # component alone ($69.28) is naturally a small slice of a GPU instance's
    # total cost — the real total across all 3 components was $631.31, only
    # ~30% under, not the reported ~92% the false per-component compare implied.
    gcp_total_by_key = {}
    for r in rows:
        gcp_total_by_key[r[5]] = gcp_total_by_key.get(r[5], 0.0) + (r[4] or 0)

    # HARD violations are cost blowups that must be clamped (not blocked).
    # WARN violations are wrong-service labels that don't inflate cost — recorded
    # for the fix backlog but cost-accurate enough to ship as-is.
    hard, warn = [], []
    seen_r5_keys = set()
    for product, svc, sku, aws, gcp, li_key, component in rows:
        aws = aws or 0.0
        gcp = gcp or 0.0
        if aws > _R1_AWS_FLOOR and gcp > _R1_RATIO * aws and gcp > _R1_GCP_FLOOR:
            hard.append((f"R1 ratio>{_R1_RATIO}x", product, svc, aws, gcp, li_key, component)); continue
        if gcp > _R2_GCP_ABS and aws < _R2_AWS_MAX:
            hard.append((f"R2 abs>${_R2_GCP_ABS/1000:.0f}k", product, svc, aws, gcp, li_key, component)); continue
        if (svc or "").strip().lower() == "cloud storage":
            pl = (product or "").lower()
            if not any(k in pl for k in _STORAGE_OK):
                # Only HARD if it also inflates; otherwise just a label warning.
                if aws > _R1_AWS_FLOOR and gcp > _R3_INFLATION * aws:
                    hard.append(("R3 non-storage->GCS (inflated)", product, svc, aws, gcp, li_key, component))
                else:
                    warn.append(("R3 non-storage->GCS", product, svc, aws, gcp, li_key, component))
        # R5 under-projection: GCP is suspiciously cheap — could be wrong SKU,
        # missing components, or unit mismatch. Flag for human review.
        # Do NOT clamp — GCP may genuinely be cheaper. Warn only.
        # Threshold: GCP < 10% of AWS on rows > $50 AWS spend. Checked ONCE per
        # aws_li_key against the item's full cost (see gcp_total_by_key above),
        # not per-component — one component being a small fraction of a
        # multi-component item's total is normal, not suspicious.
        if li_key not in seen_r5_keys:
            seen_r5_keys.add(li_key)
            gcp_item_total = gcp_total_by_key.get(li_key, 0.0)
            if aws > _R5_AWS_FLOOR and 0 < gcp_item_total < _R5_GCP_RATIO * aws:
                warn.append((f"R5 under-projection<{_R5_GCP_RATIO*100:.0f}%", product, svc, aws, gcp_item_total, li_key, component))

    bill_over = tot_aws > 0 and tot_gcp > _R4_RATIO * tot_aws
    ratio = tot_gcp / tot_aws if tot_aws else 0.0

    # A bill with ONLY R5 (under-projection) or R3-label warnings — no hard
    # clamp-worthy violation, no whole-bill blowup — used to hit this early
    # return and skip writing outlier_violations.md entirely. The warning was
    # real (this is exactly how a ~1000x-under-projected row with a wildly
    # implausible source-data unit rate slipped through undetected on a bill
    # that otherwise looked healthy) but it only ever reached a single line in
    # a background job's stdout, which nobody reads. Always write the file
    # when there's anything to say — render_report.py already surfaces
    # outlier_violations.md unconditionally if it exists, so this alone makes
    # every non-blocking warning visible in the actual customer report instead
    # of silently discarded.
    if not hard and not bill_over and not warn:
        print(f"outlier_gate: OK (AWS ${tot_aws:,.0f} -> GCP ${tot_gcp:,.0f}, ratio {ratio:.2f})")
        sys.exit(0)

    out = os.path.join(os.path.dirname(db), "outlier_violations.md")
    lines = ["# Outlier gate violations\n",
             f"AWS total ${tot_aws:,.2f} -> GCP total ${tot_gcp:,.2f} (ratio {ratio:.2f})\n"]
    if hard:
        lines.append("\nRows below were clamped to AWS-cost passthrough so the report could generate.\n")
    if bill_over:
        lines.append("- **R4 whole-bill**: GCP total exceeds 2x AWS total.\n")
    for rule, product, s, aws, gcp, _key, _comp in sorted(hard, key=lambda x: -x[4]):
        lines.append(f"- **{rule}**: {product} -> {s}  AWS ${aws:,.2f} -> GCP ${gcp:,.2f} "
                     f"({gcp/aws if aws else float('inf'):.0f}x)\n")
    if warn:
        lines.append("\n## Non-blocking warnings — not clamped, review before presenting externally\n")
        lines.append("A large gap here often traces back to the AWS source bill's own usage-quantity/cost "
                      "pairing rather than a mapping error — check the raw input row before assuming the "
                      "GCP-side SKU or rate is wrong.\n\n")
        for rule, product, s, aws, gcp, _key, _comp in sorted(warn, key=lambda x: -x[4]):
            lines.append(f"- {rule}: {product} -> {s}  ${aws:,.2f} -> ${gcp:,.2f}\n")
    with open(out, "w", encoding="utf-8") as fh:
        fh.writelines(lines)

    # Clamp violating rows to AWS-cost passthrough in the projection table.
    # This ensures the report generates with accurate totals instead of blowup numbers.
    # Scoped to (aws_li_key, component): a break_down row's core/ram/storage
    # components share one aws_li_key. Clamping by aws_li_key alone reset every
    # sibling component to passthrough whenever just one component blew up —
    # e.g. a license component's ratio blowup would also wipe out a correctly
    # sized core/RAM pair back to full AWS cost.
    bad_pairs = [(r[5], r[6]) for r in hard]
    clamped_note = ""
    if bad_pairs:
        try:
            con.executemany(
                """
                UPDATE aws_li_to_gcp_li
                SET strategy = 'passthrough',
                    gcp_sku_id = NULL, gcp_sku_name = NULL,
                    projection_note = COALESCE(projection_note || ' ', '') ||
                        '[outlier_gate: clamped to passthrough — gcp/aws ratio exceeded threshold]'
                WHERE aws_li_key = ? AND component IS NOT DISTINCT FROM ?
                """,
                bad_pairs
            )
            clamped_note = f"  {len(bad_pairs)} row(s) clamped to passthrough."
        except Exception as e:
            clamped_note = f"  WARNING: could not clamp rows ({e}) — totals may be inflated."

    if bill_over and not bad_pairs:
        clamped_note = "  R4 whole-bill flag logged; individual rows within limits."
    elif not hard and not bill_over:
        clamped_note = f"  {len(warn)} non-blocking warning(s) — nothing clamped."

    sys.stderr.write(
        f"outlier_gate: {len(hard)} blowup(s)"
        f"{' + whole-bill >2x' if bill_over else ''}. "
        f"AWS ${tot_aws:,.0f} -> GCP ${tot_gcp:,.0f}. See outlier_violations.md."
        f"{clamped_note}\n"
    )
    for rule, product, s, aws, gcp, _key, _comp in sorted(hard, key=lambda x: -x[4])[:10]:
        sys.stderr.write(f"  {rule}: {product} -> {s}  ${aws:,.2f} -> ${gcp:,.2f}\n")

    # Always exit 0 — a report with clamped rows is better than no report.
    sys.exit(0)


if __name__ == "__main__":
    main()
