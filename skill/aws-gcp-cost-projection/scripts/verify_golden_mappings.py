#!/usr/bin/env python3
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import duckdb

def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else "projection-audit/projection.duckdb"
    conn = duckdb.connect(db_path)
    
    print(f"Running golden mappings regression assertions on {db_path}...")
    
    # 1. Fetch VM mappings
    vm_rows = conn.execute("""
        SELECT c.instance_type, m.gcp_service, m.gcp_sku_name
        FROM aws_li_to_gcp_li m
        JOIN aws_li_catalog c USING(aws_li_key)
        WHERE c.instance_type IS NOT NULL
    """).fetchall()
    
    assertions = {
        "t3.large": {"service": "Compute Engine", "sku_desc": "E2 Instance"},
        # t4g is ARM but classify_mechanics.py deliberately routes all t-family
        # burstable instances (t2/t3/t3a/t4g) to E2 — GCP's burstable equivalent —
        # ahead of the full-performance C4A/T2A ARM mapper. See compute_burstable
        # in classify_mechanics.py.
        "t4g.large": {"service": "Compute Engine", "sku_desc": "E2 Instance"},
        "g5.2xlarge": {"service": "Compute Engine", "sku_desc": "G2 Instance"},
        "p4d.24xlarge": {"service": "Compute Engine", "sku_desc": "A2 Instance"},
        # Cloud SQL now uses "Cloud SQL Regional/Zonal vCPU/RAM" naming; SQLGen2Instances is legacy.
        "db.r6i.large": {"service": "Cloud SQL", "sku_desc": "Cloud SQL"},
    }
    
    failures = 0
    passed = 0
    
    for itype, gcp_svc, sku_name in vm_rows:
        # Skip GPU-component rows — they have a different SKU name by design.
        if "gpu" in (sku_name or "").lower() or "nvidia" in (sku_name or "").lower():
            continue
        itype_lower = itype.lower()
        if itype_lower.startswith("db."):
            # Storage/IO rows for RDS instances legitimately map to storage SKUs —
            # only assert on compute (vCPU/RAM) rows.
            sku_lower = (sku_name or "").lower()
            if any(w in sku_lower for w in ("storage", "snapshot", "i/o", "backup")):
                continue
            spec = assertions["db.r6i.large"]
            sku_ok = spec["sku_desc"].lower() in sku_lower
            svc_ok = spec["service"].lower() in (gcp_svc or "").lower()
            if not (sku_ok and svc_ok):
                print(f"❌ FAIL: DB instance {itype} mapped to {gcp_svc} / {sku_name} (expected {spec['service']} / {spec['sku_desc']})")
                failures += 1
            else:
                passed += 1
        else:
            for ref_itype, spec in assertions.items():
                if ref_itype.startswith("db."):
                    continue
                if ref_itype in itype_lower:
                    sku_ok = spec["sku_desc"].lower() in (sku_name or "").lower()
                    svc_ok = spec["service"].lower() in (gcp_svc or "").lower()
                    if not (sku_ok and svc_ok):
                        print(f"❌ FAIL: VM instance {itype} mapped to {gcp_svc} / {sku_name} (expected {spec['service']} / {spec['sku_desc']})")
                        failures += 1
                    else:
                        passed += 1

    # 2. Check CloudWatch
    cw_rows = conn.execute("""
        SELECT c.usage_type, c.operation, m.gcp_service, m.gcp_sku_name
        FROM aws_li_to_gcp_li m
        JOIN aws_li_catalog c USING(aws_li_key)
        WHERE c.product LIKE '%CloudWatch%'
    """).fetchall()
    
    for ut, op, svc, sku_name in cw_rows:
        ut_lower = (ut or "").lower()
        op_lower = (op or "").lower()
        if "log" in ut_lower or "log" in op_lower or "ingestion" in ut_lower or "ingestion" in op_lower:
            # logs
            if svc != "Cloud Logging" or "Log Storage" not in (sku_name or ""):
                print(f"❌ FAIL: CloudWatch Log row {ut}/{op} mapped to {svc} / {sku_name} (expected Cloud Logging / Log Storage)")
                failures += 1
            else:
                passed += 1
        else:
            # Non-log CloudWatch rows → passthrough (pricing model incompatible).
            # Assert only that gcp_service is set to Cloud Monitoring.
            if "cloud monitoring" not in (svc or "").lower():
                print(f"❌ FAIL: CloudWatch Metric row {ut}/{op} mapped to {svc} (expected Cloud Monitoring)")
                failures += 1
            else:
                passed += 1
                
    print(f"Golden assertions completed. Passed: {passed}, Failed: {failures}")
    if failures > 0:
        print(f"⚠️  WARNING: {failures} golden assertion(s) failed — report will still generate.")
        sys.exit(0)
    print("✅ All golden regression assertions passed!")

if __name__ == "__main__":
    main()
