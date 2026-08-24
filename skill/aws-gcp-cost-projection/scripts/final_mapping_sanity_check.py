#!/usr/bin/env python3
"""
final_mapping_sanity_check.py <projection.duckdb>

Deterministic safety net that runs in Phase 6, BEFORE render_report.py,
so mapping/pricing correctness is checked mechanically every run instead of
relying on an LLM noticing something during a later phase and improvising a
fix outside its assigned scope. (That happened for real: an agy agent, after
Phase 6 had already produced a report, kept going unprompted, found an RDS
Storage IOPS row it thought was mispriced, edited the shared ingest.py, and
force-restarted the whole pipeline from Phase 1 by deleting its own
checkpoint file — see orchestrate.go's OnFailurePreScript/OnFailurePrompt
comment for the unrelated ingestion-fallback mechanism, this is the
after-the-fact-review case that must never depend on the LLM's initiative.)

outlier_gate.py (Phase 5) only catches catastrophic blowups (ratio>50x AND
gcp>$25, or abs>$10k, or whole-bill>2x) — small-dollar rows with a real but
modest mapping error (a $27 AWS row 3-5x mispriced, say) never cross those
thresholds and have no other deterministic catch net. This script targets
exactly that gap for the class of error that triggered the incident above:
managed-DB (RDS/Aurora/DocumentDB/MemoryDB/ElastiCache) storage-component
rows whose product/usage_type/operation text signals a per-IOPS or
per-I/O-request charge — Cloud SQL bundles IOPS into its storage price, so
these MUST be strategy='ignore'. Anything else is a mapping bug regardless
of dollar size.

Never blocks: this only appends warnings to outlier_violations.md (already
wired into render_report.py) and always exits 0.
"""
import os
import re
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import duckdb
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"final_mapping_sanity_check: duckdb import failed ({e}); skipping\n")
    sys.exit(0)

_IOPS_PATTERN = re.compile(
    r"iops-mo|provisioned.?iops|storageiops|storage.?iops|mibps|throughput|"
    r"volumep.iops|volumep-iops|i/o request|million i.?o|million io|\bio request",
    re.IGNORECASE,
)
_MANAGED_DB_PRODUCTS = ("rds", "relational", "aurora", "documentdb", "memorydb", "elasticache")


def main():
    if len(sys.argv) < 2:
        sys.exit(0)
    db = sys.argv[1]
    con = duckdb.connect(db)

    rows = con.execute(
        """
        SELECT c.product, c.usage_type, c.operation,
               p.aws_amortized_cost, p.gcp_projected_cost,
               m.strategy, p.aws_li_key, p.component
        FROM gcp_projection p
        JOIN aws_li_catalog c USING (aws_li_key)
        JOIN aws_li_to_gcp_li m
          ON m.aws_li_key = p.aws_li_key AND m.component IS NOT DISTINCT FROM p.component
        """
    ).fetchall()

    findings = []
    for product, ut, op, aws, gcp, strategy, li_key, component in rows:
        pl = (product or "").lower()
        if not any(k in pl for k in _MANAGED_DB_PRODUCTS):
            continue
        blob = f"{ut or ''} {op or ''} {product or ''}"
        if _IOPS_PATTERN.search(blob) and (strategy or "").strip().lower() != "ignore":
            findings.append((product, ut, op, aws or 0.0, gcp or 0.0, strategy, li_key, component))

    if not findings:
        print("final_mapping_sanity_check: OK — no managed-DB IOPS/throughput rows mismapped")
        sys.exit(0)

    out = os.path.join(os.path.dirname(db), "outlier_violations.md")
    lines = ["\n## Phase 6 final mapping check — managed-DB IOPS/throughput rows not ignored\n",
             "Cloud SQL bundles I/O performance into its storage price; AWS RDS/Aurora rows "
             "billed per-IOPS or per-I/O-request should always map to strategy='ignore'. These "
             "rows didn't, and may be mispriced regardless of dollar size:\n\n"]
    for product, ut, op, aws, gcp, strategy, li_key, component in findings:
        lines.append(f"- {product} / {ut} / {op}  AWS ${aws:,.2f} -> GCP ${gcp:,.2f} "
                     f"(strategy={strategy})\n")
    with open(out, "a") as fh:
        fh.writelines(lines)

    sys.stderr.write(
        f"final_mapping_sanity_check: {len(findings)} managed-DB IOPS/throughput row(s) "
        f"not mapped to ignore — see outlier_violations.md\n"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
