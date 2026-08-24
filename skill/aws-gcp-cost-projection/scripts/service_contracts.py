"""
service_contracts.py  —  Phase J: Service Contracts.

Every AWS service has a contract: the fields the engine MUST know before it
will produce a recommendation. The validator checks the profile against the
contract before mapping starts and returns a ContractResult — if the contract
isn't satisfied, the engine either refuses to map (for critical misses) or
proceeds with reduced confidence (for optional/unavailable fields).

The contract enforcement gate is cleaner than scattered `if field is None`
checks inside each strategy. It also makes it obvious in the output exactly
what information is missing and why, rather than silently defaulting.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from field_state import (
    FIELD_REGISTRY, FieldSource, FieldState,
    build_field_state_map, data_confidence_from_state_map,
)


# ---------------------------------------------------------------------------
# Contract definition
# ---------------------------------------------------------------------------

@dataclass
class ServiceContract:
    service: str
    critical_fields: list[str]   # missing any of these → refuse to map
    important_fields: list[str]  # missing these → confidence penalty + assumption emitted
    optional_fields: list[str]   # missing these → noted but no penalty


# One contract per service. These define what the engine actually needs —
# not what would be nice to have, but what will produce a wrong answer if absent.
CONTRACTS: dict[str, ServiceContract] = {

    "EC2": ServiceContract(
        service="EC2",
        critical_fields=["instance_type", "vcpu", "ram_gb", "architecture", "region"],
        important_fields=["os", "license_model", "network_tier", "local_nvme_gb",
                          "gpu_count", "gpu_model", "gpu_vram_gb"],
        optional_fields=["tenancy", "purchase_model", "efa_supported", "bare_metal",
                         "ebs_iops", "ebs_throughput_mbps"],
    ),

    "EC2_burstable": ServiceContract(
        service="EC2_burstable",
        critical_fields=["instance_type", "vcpu", "ram_gb", "architecture", "region"],
        important_fields=["cpu_credits_charged"],
        # avg_cpu_utilization and unlimited_mode are both NOT_AVAILABLE_CUR_ONLY —
        # they'll show up as UNAVAILABLE in the state map, reducing data confidence
        # and triggering a migration assumption, but they don't block mapping.
        optional_fields=["avg_cpu_utilization", "unlimited_mode", "purchase_model"],
    ),

    "RDS": ServiceContract(
        service="RDS",
        critical_fields=["instance_type", "vcpu", "ram_gb", "region", "engine",
                         "license_model", "multi_az"],
        important_fields=["engine_version", "edition", "storage_type", "provisioned_iops"],
        # backup_retention_days and read_replica_count are NOT_AVAILABLE_CUR_ONLY —
        # appear as UNAVAILABLE, emit assumptions, but don't block mapping.
        optional_fields=["allocated_storage_gb", "backup_retention_days", "read_replica_count"],
    ),

    "ElastiCache": ServiceContract(
        service="ElastiCache",
        critical_fields=["instance_type", "region", "engine"],
        important_fields=["engine_version"],
        optional_fields=["cluster_mode"],  # NOT_AVAILABLE_CUR_ONLY
    ),

    "MSK": ServiceContract(
        service="MSK",
        critical_fields=["broker_type", "broker_count", "region"],
        important_fields=["kafka_version", "allocated_storage_gb"],
        # replication_factor is NOT_AVAILABLE_CUR_ONLY
        optional_fields=["replication_factor"],
    ),

    "OpenSearch": ServiceContract(
        service="OpenSearch",
        critical_fields=["instance_type", "vcpu", "ram_gb", "region"],
        important_fields=["allocated_storage_gb", "ebs_iops"],
        optional_fields=[],
    ),

    "Lambda": ServiceContract(
        service="Lambda",
        critical_fields=["region"],
        important_fields=["ram_gb", "architecture"],
        optional_fields=["purchase_model"],
    ),

    "EKS": ServiceContract(
        service="EKS",
        # EKS billing in CUR is mostly the cluster management fee; underlying EC2
        # nodes appear as separate EC2 line items so the EC2 contract handles them.
        critical_fields=["region"],
        important_fields=[],
        optional_fields=["purchase_model"],
    ),
}


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------

@dataclass
class FieldResult:
    field_name: str
    state: str
    source: str
    value: Optional[object]
    note: Optional[str] = None


@dataclass
class ContractResult:
    service: str
    passed: bool
    blocked_reason: Optional[str]            # set if critical fields missing
    data_confidence: float
    field_results: list[FieldResult]
    unavailable_fields: list[str]            # NOT_AVAILABLE_CUR_ONLY fields encountered
    unknown_required_fields: list[str]       # fields that should exist but don't

    def summary(self) -> str:
        lines = [f"Contract [{self.service}]: {'PASS' if self.passed else 'BLOCKED'}"]
        if self.blocked_reason:
            lines.append(f"  Reason: {self.blocked_reason}")
        lines.append(f"  Data confidence: {self.data_confidence:.0%}")
        if self.unavailable_fields:
            lines.append(f"  CUR-unavailable (external API needed): {self.unavailable_fields}")
        if self.unknown_required_fields:
            lines.append(f"  Unknown required fields (data gap): {self.unknown_required_fields}")
        for fr in self.field_results:
            icon = "✓" if fr.state in ("known", "derived") else ("⚠" if fr.state == "unavailable" else "✗")
            lines.append(f"  {icon} {fr.field_name}: {fr.state} [{fr.source}]"
                         + (f" — {fr.note}" if fr.note else ""))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def validate_contract(service: str, profile: dict) -> ContractResult:
    """
    Validates a workload profile against the service contract.
    Returns a ContractResult. If passed=False, the pipeline should route
    the row to the outlier flow rather than attempting mapping.
    """
    contract = CONTRACTS.get(service) or CONTRACTS.get("EC2")  # fallback
    all_fields = (contract.critical_fields
                  + contract.important_fields
                  + contract.optional_fields)

    state_map = build_field_state_map(profile, all_fields)
    data_conf = data_confidence_from_state_map(state_map, contract.critical_fields
                                               + contract.important_fields)

    field_results = []
    unavailable = []
    unknown_required = []

    for fname, entry in state_map.items():
        fr = FieldResult(
            field_name=fname,
            state=entry["state"],
            source=entry["source"],
            value=entry["value"],
            note=entry["note"],
        )
        field_results.append(fr)
        if entry["state"] == FieldState.UNAVAILABLE.value:
            unavailable.append(fname)
        if entry["state"] == FieldState.UNKNOWN.value and fname in contract.critical_fields:
            unknown_required.append(fname)

    blocked_reason = None
    if unknown_required:
        blocked_reason = f"Critical fields missing: {unknown_required}"

    return ContractResult(
        service=service,
        passed=(blocked_reason is None),
        blocked_reason=blocked_reason,
        data_confidence=data_conf,
        field_results=field_results,
        unavailable_fields=unavailable,
        unknown_required_fields=unknown_required,
    )


# ---------------------------------------------------------------------------
# Production helper: determine which contract applies for a given AWS service
# ---------------------------------------------------------------------------

# Maps the `product` field values found in aws_li_catalog to contract keys.
# Burstable detection is done by instance_type prefix at runtime.
_PRODUCT_TO_CONTRACT: dict[str, str] = {
    "Amazon Elastic Compute Cloud": "EC2",
    "Amazon EC2": "EC2",
    "EC2": "EC2",
    "Amazon Relational Database Service": "RDS",
    "Amazon RDS": "RDS",
    "RDS": "RDS",
    "Amazon ElastiCache": "ElastiCache",
    "ElastiCache": "ElastiCache",
    "Amazon Managed Streaming for Apache Kafka": "MSK",
    "Amazon MSK": "MSK",
    "MSK": "MSK",
    "Amazon OpenSearch Service": "OpenSearch",
    "OpenSearch": "OpenSearch",
    "Amazon Elasticsearch Service": "OpenSearch",
    "AWS Lambda": "Lambda",
    "Lambda": "Lambda",
    "Amazon Elastic Kubernetes Service": "EKS",
    "Amazon EKS": "EKS",
    "EKS": "EKS",
}

_BURSTABLE_PREFIXES = ("t2.", "t3.", "t3a.", "t4g.")


def _resolve_contract_key(product: str, row: dict) -> str:
    """Return the contract key for a catalog row."""
    base = _PRODUCT_TO_CONTRACT.get(product, "EC2")
    if base == "EC2":
        instance_type = (row.get("instance_type") or "").lower()
        if any(instance_type.startswith(p) for p in _BURSTABLE_PREFIXES):
            return "EC2_burstable"
    return base


# ---------------------------------------------------------------------------
# Bulk validator: validate_all_contracts
# ---------------------------------------------------------------------------

def validate_all_contracts(db_path: str) -> dict:
    """
    Opens projection.duckdb at db_path, reads aws_li_catalog rows grouped by
    service (product field), validates each row against its service contract,
    and writes contract_issues / field_availability back to the table.

    Returns a summary dict:
        {
            "total_rows": int,
            "contract_failures": int,
            "unavailable_field_rows": int,
            "details_by_service": {
                "<service>": {
                    "total": int,
                    "failures": int,
                    "unavailable_field_rows": int,
                    "failure_examples": [ {"rowid": ..., "issues": [...]} ]
                }
            }
        }
    """
    import json
    import duckdb

    con = duckdb.connect(db_path)

    # Ensure the two new columns exist.
    for ddl in (
        "ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS contract_issues TEXT",
        "ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS field_availability TEXT",
    ):
        try:
            con.execute(ddl)
        except Exception:
            pass  # column may already exist in some DuckDB versions without IF NOT EXISTS

    # Fetch all rows as dicts. DuckDB returns results as list-of-tuples; use
    # fetchdf() for convenience.
    df = con.execute("SELECT * FROM aws_li_catalog").fetchdf()

    total_rows = len(df)
    contract_failures = 0
    unavailable_field_rows = 0
    details_by_service: dict[str, dict] = {}

    # Collect updates to apply in bulk.
    updates: list[tuple[str | None, str | None, int]] = []  # (issues_json, avail, rowid)

    for idx, row in df.iterrows():
        row_dict = row.to_dict()
        product = str(row_dict.get("product") or "")
        contract_key = _resolve_contract_key(product, row_dict)

        result = validate_contract(contract_key, row_dict)

        # Determine per-row annotations.
        issues_json: str | None = None
        avail: str | None = None

        if not result.passed:
            contract_failures += 1
            missing = result.unknown_required_fields
            issues_json = json.dumps(missing)

        if result.unavailable_fields:
            unavailable_field_rows += 1
            avail = "partial"

        # Collect the rowid for targeted UPDATE.
        rowid = int(row_dict.get("rowid", idx))
        updates.append((issues_json, avail, rowid))

        # Per-service stats.
        svc_stats = details_by_service.setdefault(contract_key, {
            "total": 0,
            "failures": 0,
            "unavailable_field_rows": 0,
            "failure_examples": [],
        })
        svc_stats["total"] += 1
        if not result.passed:
            svc_stats["failures"] += 1
            if len(svc_stats["failure_examples"]) < 5:
                svc_stats["failure_examples"].append({
                    "rowid": rowid,
                    "issues": result.unknown_required_fields,
                })
        if result.unavailable_fields:
            svc_stats["unavailable_field_rows"] += 1

    # Write annotations back to DuckDB using a temporary staging table.
    if updates:
        # Build a VALUES clause for a bulk update.
        con.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _contract_updates "
            "(rowid BIGINT, contract_issues TEXT, field_availability TEXT)"
        )
        con.execute("DELETE FROM _contract_updates")
        con.executemany(
            "INSERT INTO _contract_updates VALUES (?, ?, ?)",
            [(rowid, issues_json, avail) for issues_json, avail, rowid in updates],
        )
        con.execute(
            """
            UPDATE aws_li_catalog AS t
            SET
                contract_issues   = u.contract_issues,
                field_availability = u.field_availability
            FROM _contract_updates AS u
            WHERE t.rowid = u.rowid
            """
        )

    con.close()

    return {
        "total_rows": total_rows,
        "contract_failures": contract_failures,
        "unavailable_field_rows": unavailable_field_rows,
        "details_by_service": details_by_service,
    }


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: service_contracts.py <path-to-projection.duckdb>", file=sys.stderr)
        sys.exit(1)

    result = validate_all_contracts(sys.argv[1])
    print(json.dumps(result, indent=2))
    if result["contract_failures"] > 0:
        print(f"[WARN] {result['contract_failures']} rows failed service contract validation")
