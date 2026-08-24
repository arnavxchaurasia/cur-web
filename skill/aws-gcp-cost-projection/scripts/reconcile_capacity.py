#!/usr/bin/env python3
"""
reconcile_capacity.py — Eliminate vCPU/RAM under-provisioning, post-merge.

For map/break_down rows where the mapped GCP unit_multiplier (vCPU or RAM) is
less than the AWS instance's actual spec, bumps it to the AWS spec and tags
the row with a capacity-adjusted note. NOTE: family_mapper.py (the live
compute-sizing path) tags its core/ram output 'map', not 'break_down' — an
earlier version of this check only matched 'break_down', which no mapper
actually emits, so this guarantee silently enforced nothing. Both strategies
are checked now.

Rule: GCP capacity must be >= AWS capacity, per-row. Even a 1-vCPU deficit
is a real under-provision that violates the "never underprovision" guarantee.
This script enforces zero tolerance; auto_review.py has a >0.5 tolerance
that lets small deficits through.

Runs as Phase 2 post_llm_script, after calibrate_confidence.py.
"""

import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import duckdb


def main():
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

    # ── vCPU ──────────────────────────────────────────────────────────────────
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
                    ' → ' || CAST(ROUND(?, 2) AS VARCHAR) || ' to match AWS ' || ? || ']'
            WHERE aws_li_key = ? AND strategy IN ('break_down', 'map') AND component = 'core'
        """, [aws_v, gcp_v, aws_v, itype or "instance", key])
        vcpu_gain += aws_v - gcp_v

    # ── RAM ───────────────────────────────────────────────────────────────────
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
                    ' GB → ' || CAST(ROUND(?, 1) AS VARCHAR) ||
                    ' GB to match AWS ' || ? || ']'
            WHERE aws_li_key = ? AND strategy IN ('break_down', 'map') AND component = 'ram'
        """, [aws_r, gcp_r, aws_r, itype or "instance", key])
        ram_gain += aws_r - gcp_r

    con.commit()
    con.close()

    if under_vcpu:
        print(f"reconcile_capacity: vCPU: {len(under_vcpu)} row(s) upsized "
              f"(+{vcpu_gain:.2f} vCPU recovered)")
    if under_ram:
        print(f"reconcile_capacity: RAM:  {len(under_ram)} row(s) upsized "
              f"(+{ram_gain:.2f} GB recovered)")
    if not under_vcpu and not under_ram:
        print("reconcile_capacity: no capacity deficits found — all rows meet or exceed AWS specs")


if __name__ == "__main__":
    main()
