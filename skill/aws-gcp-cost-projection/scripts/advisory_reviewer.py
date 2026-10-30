#!/usr/bin/env python3
"""
advisory_reviewer.py — Post-projection advisory reviewer pass. STRICTLY READ-ONLY.

Emits structured findings into projection-audit/review_findings.json:
[
  {
    "job_id": "...",
    "aws_li_key": "...",
    "canonical_service": "...",
    "charge_type": "...",
    "finding_type": "missing_spec|suspect_ratio|target_not_in_allowed|passthrough_should_be_mapped|unit_mismatch|stale_rate",
    "evidence": {
      "aws_od": 0.0,
      "gcp_od": 0.0,
      "ratio": 0.0,
      "rule_or_spec_hit": "...",
      "product": "...",
      "usage_type": "...",
      "gcp_service": "...",
      "gcp_sku_name": "..."
    },
    "severity": "HIGH|MEDIUM|LOW",
    "suggested_action": "..."
  }
]
"""
from __future__ import annotations

import json
import os
import re
import sys
import duckdb

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.environ.get("SKILL_DIR") or os.path.dirname(_HERE)
sys.path.insert(0, _SKILL_DIR)
sys.path.insert(0, os.path.join(_SKILL_DIR, "scripts"))

try:
    from aws_normalizer import canonical_service
except Exception:
    def canonical_service(p):
        return (p or "").lower().replace("amazon ", "").replace("aws ", "").strip()

try:
    from config_loader import load_data_config as _cfg
except Exception:
    def _cfg(name):
        return {}


def load_allowed_gcp_targets() -> set[str]:
    service_map = _cfg("service_map") or {}
    targets = set()
    for rule in service_map.get("rules", []):
        tgt = rule.get("gcp_service")
        if tgt and not tgt.startswith("N/A"):
            targets.add(tgt.strip())
    # Standard GCP services
    targets.update([
        "Compute Engine", "Cloud Storage", "Cloud SQL", "Cloud Memorystore",
        "Cloud Memorystore for Redis", "Cloud Memorystore for Memcached",
        "Cloud Spanner", "Cloud Bigtable", "Cloud Pub/Sub", "Cloud Logging",
        "Cloud Monitoring", "Cloud NAT", "Cloud DNS", "Cloud Load Balancing",
        "Filestore", "Cloud Run", "Google Kubernetes Engine", "BigQuery",
        "Dataproc", "Dataflow", "Cloud Armor", "Secret Manager", "Cloud KMS",
        "Manual Review", "Manual Sizing Required", "Passthrough",
    ])
    return targets


def run_advisory_review(job_dir: str) -> list[dict]:
    db_path = os.path.join(job_dir, "projection-audit", "projection.duckdb")
    if not os.path.exists(db_path):
        db_path = os.path.join(job_dir, "projection.duckdb")
        if not os.path.exists(db_path):
            print(f"[advisory_reviewer] Database not found in {job_dir}, skipping.")
            return []

    job_id = os.path.basename(os.path.abspath(job_dir))
    allowed_targets = load_allowed_gcp_targets()

    findings: list[dict] = []

    con = duckdb.connect(db_path, read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if "aws_li_catalog" not in tables or "gcp_projection" not in tables:
            print("[advisory_reviewer] Missing catalog or projection table, skipping.")
            return []

        c_cols = {c[1].lower() for c in con.execute("PRAGMA table_info('aws_li_catalog')").fetchall()}
        has_aws_od = "aws_od_cost" in c_cols

        query = f"""
            SELECT
                c.aws_li_key,
                c.product,
                c.usage_type,
                c.pricing_unit,
                c.aws_amortized_cost,
                {"c.aws_od_cost" if has_aws_od else "c.aws_amortized_cost"} AS aws_od,
                c.instance_type,
                p.gcp_service,
                p.gcp_sku_name,
                p.gcp_sku_id,
                p.strategy,
                p.unit_multiplier,
                p.gcp_projected_cost,
                p.gcp_od_cost,
                p.projection_note
            FROM aws_li_catalog c
            JOIN gcp_projection p USING (aws_li_key)
            WHERE c.is_workload = TRUE
        """
        rows = con.execute(query).fetchall()

        for (
            aws_li_key, product, usage_type, pricing_unit, aws_amortized, aws_od,
            instance_type, gcp_service, gcp_sku_name, gcp_sku_id,
            strategy, unit_multiplier, gcp_projected, gcp_od, proj_note
        ) in rows:
            aws_od_val = float(aws_od or 0.0)
            gcp_od_val = float(gcp_od or 0.0)
            canon = canonical_service(product) or "unknown"
            ut_lower = (usage_type or "").lower()
            prod_lower = (product or "").lower()
            unit_lower = (pricing_unit or "").lower()

            # Determine charge type
            charge_type = "usage"
            if "hour" in ut_lower or "boxusage" in ut_lower or "node" in ut_lower:
                charge_type = "instance_hours"
            elif "byte" in ut_lower or "storage" in ut_lower or "gb" in ut_lower:
                charge_type = "storage_gb"
            elif "request" in ut_lower or "api" in ut_lower:
                charge_type = "requests"

            # -------------------------------------------------------------
            # 1. missing_spec: compute break_down missing spec or multiplier 0
            # -------------------------------------------------------------
            if strategy in ("break_down", "map") and ("ec2" in canon or "compute" in prod_lower or "rds" in canon):
                if ("boxusage" in ut_lower or "instance" in ut_lower) and (unit_multiplier is None or unit_multiplier <= 0):
                    findings.append({
                        "job_id": job_id,
                        "aws_li_key": aws_li_key,
                        "canonical_service": canon,
                        "charge_type": charge_type,
                        "finding_type": "missing_spec",
                        "evidence": {
                            "aws_od": round(aws_od_val, 2),
                            "gcp_od": round(gcp_od_val, 2),
                            "unit_multiplier": unit_multiplier,
                            "instance_type": instance_type,
                            "rule_or_spec_hit": "compute row has 0 or missing unit_multiplier",
                            "product": product,
                            "usage_type": usage_type,
                            "gcp_service": gcp_service,
                            "gcp_sku_name": gcp_sku_name,
                        },
                        "severity": "HIGH" if aws_od_val > 10 else "MEDIUM",
                        "suggested_action": f"Resolve vCPU / RAM specs for {instance_type or 'instance'} and update unit_multiplier.",
                    })

            # -------------------------------------------------------------
            # 2. suspect_ratio: ratio > 3.0 or < 0.20 on material spend (>$5)
            # -------------------------------------------------------------
            if aws_od_val >= 5.0 and strategy in ("map", "break_down") and gcp_od_val > 0:
                ratio = gcp_od_val / aws_od_val
                if ratio > 3.0 or ratio < 0.20:
                    sev = "HIGH" if aws_od_val >= 50 or ratio > 5.0 or ratio < 0.10 else "MEDIUM"
                    findings.append({
                        "job_id": job_id,
                        "aws_li_key": aws_li_key,
                        "canonical_service": canon,
                        "charge_type": charge_type,
                        "finding_type": "suspect_ratio",
                        "evidence": {
                            "aws_od": round(aws_od_val, 2),
                            "gcp_od": round(gcp_od_val, 2),
                            "ratio": round(ratio, 3),
                            "rule_or_spec_hit": f"od ratio {ratio:.2f}x outside [0.20, 3.0] bounds",
                            "product": product,
                            "usage_type": usage_type,
                            "gcp_service": gcp_service,
                            "gcp_sku_name": gcp_sku_name,
                        },
                        "severity": sev,
                        "suggested_action": "Verify SKU tier and unit multiplier; inspect whether pricing unit conversion is aligned.",
                    })

            # -------------------------------------------------------------
            # 3. target_not_in_allowed: gcp_service not in allowed enum
            # -------------------------------------------------------------
            if gcp_service and gcp_service.strip():
                clean_target = gcp_service.strip()
                if clean_target.startswith("Amazon") or clean_target.startswith("AWS") or (clean_target not in allowed_targets and not any(clean_target.lower() == t.lower() for t in allowed_targets)):
                    findings.append({
                        "job_id": job_id,
                        "aws_li_key": aws_li_key,
                        "canonical_service": canon,
                        "charge_type": charge_type,
                        "finding_type": "target_not_in_allowed",
                        "evidence": {
                            "aws_od": round(aws_od_val, 2),
                            "gcp_od": round(gcp_od_val, 2),
                            "gcp_service": gcp_service,
                            "rule_or_spec_hit": f"Target '{gcp_service}' is echo or not in allowed GCP target set",
                            "product": product,
                            "usage_type": usage_type,
                            "gcp_sku_name": gcp_sku_name,
                        },
                        "severity": "HIGH",
                        "suggested_action": "Remap gcp_service to a valid canonical GCP equivalent service name.",
                    })

            # -------------------------------------------------------------
            # 4. passthrough_should_be_mapped: passthrough on core mappable services
            # -------------------------------------------------------------
            mappable_canons = {"ec2", "s3", "rds", "ebs", "elasticache", "elb", "natgateway"}
            if strategy == "passthrough" and (canon in mappable_canons or any(m in prod_lower for m in ["elastic compute", "relational database", "simple storage", "elastic block"])):
                is_intentional = proj_note and any(x in proj_note.lower() for x in ["no gcp equivalent", "no direct gcp equivalent", "manual review"])
                if not is_intentional and aws_od_val >= 5.0:
                    findings.append({
                        "job_id": job_id,
                        "aws_li_key": aws_li_key,
                        "canonical_service": canon,
                        "charge_type": charge_type,
                        "finding_type": "passthrough_should_be_mapped",
                        "evidence": {
                            "aws_od": round(aws_od_val, 2),
                            "gcp_od": round(gcp_od_val, 2),
                            "rule_or_spec_hit": "core workload service left as unreviewed passthrough",
                            "product": product,
                            "usage_type": usage_type,
                            "gcp_service": gcp_service,
                            "gcp_sku_name": gcp_sku_name,
                        },
                        "severity": "HIGH" if aws_od_val >= 50 else "MEDIUM",
                        "suggested_action": f"Provide deterministic mapping for {canon} charge.",
                    })

            # -------------------------------------------------------------
            # 5. unit_mismatch: e.g. requests billed with hourly sku or vice versa
            # -------------------------------------------------------------
            if strategy in ("map", "break_down") and gcp_sku_name:
                sku_lower = gcp_sku_name.lower()
                if "request" in unit_lower and ("hour" in sku_lower or "storage" in sku_lower) and "request" not in sku_lower:
                    findings.append({
                        "job_id": job_id,
                        "aws_li_key": aws_li_key,
                        "canonical_service": canon,
                        "charge_type": charge_type,
                        "finding_type": "unit_mismatch",
                        "evidence": {
                            "aws_od": round(aws_od_val, 2),
                            "gcp_od": round(gcp_od_val, 2),
                            "pricing_unit": pricing_unit,
                            "gcp_sku_name": gcp_sku_name,
                            "rule_or_spec_hit": f"pricing unit '{pricing_unit}' mismatched with sku '{gcp_sku_name}'",
                            "product": product,
                            "usage_type": usage_type,
                        },
                        "severity": "HIGH",
                        "suggested_action": "Match request-based AWS usage to a per-request GCP SKU.",
                    })

            # -------------------------------------------------------------
            # 6. stale_rate: unpriced mapped row or missing SKU rate
            # -------------------------------------------------------------
            if strategy in ("map", "break_down") and (gcp_projected is None or (gcp_projected == 0 and aws_od_val > 10)):
                findings.append({
                    "job_id": job_id,
                    "aws_li_key": aws_li_key,
                    "canonical_service": canon,
                    "charge_type": charge_type,
                    "finding_type": "stale_rate",
                    "evidence": {
                        "aws_od": round(aws_od_val, 2),
                        "gcp_od": round(gcp_od_val, 2),
                        "gcp_projected_cost": gcp_projected,
                        "gcp_sku_id": gcp_sku_id,
                        "rule_or_spec_hit": "mapped row projected to 0 or null cost despite positive AWS spend",
                        "product": product,
                        "usage_type": usage_type,
                        "gcp_service": gcp_service,
                    },
                    "severity": "HIGH",
                    "suggested_action": "Check catalog coverage for this SKU / region and refresh rate card.",
                })

    finally:
        con.close()

    # Sort findings by $ exposure descending (aws_od)
    findings.sort(key=lambda f: -f["evidence"].get("aws_od", 0.0))

    audit_dir = os.path.join(job_dir, "projection-audit")
    os.makedirs(audit_dir, exist_ok=True)
    findings_json = os.path.join(audit_dir, "review_findings.json")
    findings_jsonl = os.path.join(audit_dir, "review_findings.jsonl")

    with open(findings_json, "w", encoding="utf-8") as f:
        json.dump(findings, f, indent=2)

    with open(findings_jsonl, "w", encoding="utf-8") as f:
        for item in findings:
            f.write(json.dumps(item) + "\n")

    print(f"[advisory_reviewer] Wrote {len(findings)} structured finding(s) to {findings_json}")
    return findings


def main():
    job_dir = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    run_advisory_review(job_dir)


if __name__ == "__main__":
    main()
