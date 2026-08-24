#!/usr/bin/env python3
"""
fix_storage_misroute.py <projection.duckdb>

Deterministic repair for the CloudTrail-class bug: Cloud Storage (GCS) is OBJECT
storage. Any row whose AWS product is NOT an object-storage service but was mapped
to Cloud Storage is mis-routed — that is how CloudTrail's 790,600 events became a
$38,247 "Standard Storage" line.

Confident, non-inflating fix: convert those rows to strategy='passthrough' so they
carry their AWS cost 1:1 (never an invented GCP storage cost) and stamp a
"needs manual review" note. This removes the entire blowup class without pretending
to know the correct GCP target — the precise per-service mappings (CloudTrail->Cloud
Logging, EMR->Dataproc, EBS->Persistent Disk, EFS->Filestore) are done later, one at
a time, after research.

Idempotent. Never fatal — prints what it changed and exits 0.
"""
import os, sys
try:
    import duckdb
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"fix_storage_misroute: duckdb import failed ({e}); skipping\n")
    sys.exit(0)

# AWS products that legitimately map to GCS object storage (substring, case-insens).
# NB: do NOT use bare "s3" — it false-matches region codes like "APS3" in a
# product name (e.g. "AWS CloudTrail APS3-InsightsEvents").
_STORAGE_OK = ("simple storage service", "glacier", "storage gateway")


def main():
    if len(sys.argv) < 2:
        sys.exit(0)
    db = sys.argv[1]
    con = duckdb.connect(db)

    # Candidates: mapped to Cloud Storage but product isn't object storage.
    cond_not_storage = " AND ".join(
        [f"lower(c.product) NOT LIKE '%{k}%'" for k in _STORAGE_OK]
    )
    rows = con.execute(
        f"""
        SELECT m.aws_li_key, c.product
        FROM aws_li_to_gcp_li m
        JOIN aws_li_catalog c USING (aws_li_key)
        WHERE lower(trim(m.gcp_service)) = 'cloud storage'
          AND m.strategy <> 'passthrough'
          AND {cond_not_storage}
        """
    ).fetchall()

    if not rows:
        print("fix_storage_misroute: no non-storage rows mapped to Cloud Storage")
        sys.exit(0)

    note = ("MANUAL REVIEW: auto-rerouted from Cloud Storage (not an object-storage "
            "service); carried at AWS cost pending a researched GCP mapping.")
    keys = [r[0] for r in rows]
    con.executemany(
        """
        UPDATE aws_li_to_gcp_li
        SET strategy = 'passthrough',
            gcp_sku_id = NULL,
            mapping_confidence = 20,
            projection_note = ?
        WHERE aws_li_key = ?
        """,
        [(note, k) for k in keys],
    )

    from collections import Counter
    by_prod = Counter(p for _, p in rows)
    print(f"fix_storage_misroute: rerouted {len(rows)} non-storage row(s) off Cloud Storage -> passthrough")
    for prod, n in by_prod.most_common(12):
        print(f"  {n:4d}  {prod}")
    sys.exit(0)


if __name__ == "__main__":
    main()
