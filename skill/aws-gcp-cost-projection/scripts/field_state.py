"""
field_state.py  —  Phase G/I combined: data governance + field state tracking.

The most important thing this file does is honestly annotate which fields
*cannot* be obtained from CUR alone. Several fields in the original
extraction_profiles.yaml were listed as if ingest.py could just pull them —
cuda_version, numa_nodes, avg_cpu_utilization, backup_retention_days — when
those values come from CloudWatch, Describe* APIs, or runtime inspection.

Annotating this explicitly means:
  - ingest.py stops pretending it has data it doesn't
  - confidence scores can penalize missing required fields honestly
  - the final report says "we assumed X because Y was unavailable" instead
    of silently treating UNKNOWN as a valid input to mapping logic

Also stores catalog version stamps inside every recommendation so outputs
are reproducible — the same CUR row + same catalog versions always produce
the same result, which matters when customers re-run reports after updates.
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class FieldState(Enum):
    KNOWN         = "known"          # confirmed value from a reliable source
    DERIVED       = "derived"        # computed from other known fields
    UNKNOWN       = "unknown"        # field schema exists but value not found
    UNAVAILABLE   = "unavailable"    # source fundamentally cannot provide this
    NOT_APPLICABLE = "n/a"           # irrelevant for this resource (e.g. gpu_count on t3)


class FieldSource(Enum):
    CUR                   = "cur"
    AWS_CATALOG           = "aws_catalog"       # describe-instance-types API
    GCP_CATALOG           = "gcp_catalog"       # your existing catalog.duckdb
    DERIVED               = "derived"           # computed from other fields
    NOT_AVAILABLE_CUR_ONLY = "not_available_cur_only"  # needs external API, not billing data


# ---------------------------------------------------------------------------
# Field definitions
# ---------------------------------------------------------------------------

@dataclass
class FieldDef:
    name: str
    source: FieldSource
    required: bool = True
    state_if_missing: FieldState = FieldState.UNKNOWN
    note: Optional[str] = None


# Every field the engine touches, with where it actually comes from.
# NOT_AVAILABLE_CUR_ONLY fields are not silently omitted — they appear
# explicitly in the state map with state=UNAVAILABLE and propagate into
# data_confidence and the migration assumptions output.
#
# NOTE: "ram_gb" and "memory_gb" are treated as aliases for the same field.
# CUR reports memory as "product/memory" which ingest.py may store as either
# ram_gb or memory_gb depending on schema version. build_field_state_map()
# checks for both and uses whichever is present.
FIELD_REGISTRY: dict[str, FieldDef] = {

    # ── CUR-derivable ──────────────────────────────────────────────────────
    "instance_type":       FieldDef("instance_type",       FieldSource.CUR, required=True),
    "vcpu":                FieldDef("vcpu",                FieldSource.CUR, required=True,
                                    note="product/vcpu — fall back to aws_catalog if blank"),
    "ram_gb":              FieldDef("ram_gb",              FieldSource.CUR, required=True,
                                    note="product/memory — fall back to aws_catalog if blank; "
                                         "alias: memory_gb"),
    "region":              FieldDef("region",              FieldSource.CUR, required=True),
    "os":                  FieldDef("os",                  FieldSource.CUR, required=False,
                                    note="product/operatingSystem"),
    "license_model":       FieldDef("license_model",       FieldSource.CUR, required=False,
                                    state_if_missing=FieldState.UNKNOWN,
                                    note="product/licenseModel — BYOL vs License Included"),
    "tenancy":             FieldDef("tenancy",             FieldSource.CUR, required=False),
    "purchase_model":      FieldDef("purchase_model",      FieldSource.CUR, required=False,
                                    note="lineItem/LineItemType: OnDemand | Reserved | SavingsPlan"),
    "ebs_iops":            FieldDef("ebs_iops",            FieldSource.CUR, required=False,
                                    note="separate provisioned IOPS line item"),
    "ebs_throughput_mbps": FieldDef("ebs_throughput_mbps", FieldSource.CUR, required=False),
    "cpu_credits_charged": FieldDef("cpu_credits_charged", FieldSource.CUR, required=False,
                                    note="CPUCredits:* usage type line items for T-series"),
    # RDS CUR fields
    "engine":              FieldDef("engine",              FieldSource.CUR, required=False,
                                    note="product/databaseEngine"),
    "engine_version":      FieldDef("engine_version",      FieldSource.CUR, required=False),
    "edition":             FieldDef("edition",             FieldSource.CUR, required=False,
                                    note="product/databaseEdition"),
    "multi_az":            FieldDef("multi_az",            FieldSource.CUR, required=False,
                                    note="product/deploymentOption: Multi-AZ | Single-AZ"),
    "storage_type":        FieldDef("storage_type",        FieldSource.CUR, required=False,
                                    note="product/storageType: gp2|gp3|io1|aurora"),
    "allocated_storage_gb":FieldDef("allocated_storage_gb",FieldSource.DERIVED, required=False),
    "provisioned_iops":    FieldDef("provisioned_iops",    FieldSource.CUR, required=False,
                                    note="separate RDS IOPS line item; absent for gp2"),
    # MSK CUR fields
    "broker_type":         FieldDef("broker_type",         FieldSource.CUR, required=False),
    "broker_count":        FieldDef("broker_count",        FieldSource.DERIVED, required=False,
                                    note="count of MSK broker line items in CUR"),
    "kafka_version":       FieldDef("kafka_version",       FieldSource.CUR, required=False,
                                    note="product/softwareVersion where available"),

    # ── AWS Catalog-derivable (describe-instance-types) ─────────────────────
    "architecture":        FieldDef("architecture",        FieldSource.AWS_CATALOG, required=True),
    "gpu_count":           FieldDef("gpu_count",           FieldSource.AWS_CATALOG, required=False,
                                    state_if_missing=FieldState.NOT_APPLICABLE),
    "gpu_model":           FieldDef("gpu_model",           FieldSource.AWS_CATALOG, required=False,
                                    state_if_missing=FieldState.NOT_APPLICABLE),
    "gpu_vram_gb":         FieldDef("gpu_vram_gb",         FieldSource.AWS_CATALOG, required=False,
                                    state_if_missing=FieldState.NOT_APPLICABLE),
    "local_nvme_gb":       FieldDef("local_nvme_gb",       FieldSource.AWS_CATALOG, required=False,
                                    state_if_missing=FieldState.NOT_APPLICABLE),
    "efa_supported":       FieldDef("efa_supported",       FieldSource.AWS_CATALOG, required=False),
    "network_tier":        FieldDef("network_tier",        FieldSource.AWS_CATALOG, required=False),
    "burstable":           FieldDef("burstable",           FieldSource.AWS_CATALOG, required=False),
    "bare_metal":          FieldDef("bare_metal",          FieldSource.AWS_CATALOG, required=False),

    # ── NOT available from CUR alone — external API required ────────────────
    # These are explicitly marked rather than silently absent. Any strategy
    # that lists these as "required" will immediately get data_confidence < 1.0
    # and the missing fields will appear in the migration assumptions output.
    "avg_cpu_utilization": FieldDef("avg_cpu_utilization",
                                    FieldSource.NOT_AVAILABLE_CUR_ONLY, required=False,
                                    state_if_missing=FieldState.UNAVAILABLE,
                                    note="CloudWatch GetMetricStatistics — not in billing data"),
    "unlimited_mode":      FieldDef("unlimited_mode",
                                    FieldSource.NOT_AVAILABLE_CUR_ONLY, required=False,
                                    state_if_missing=FieldState.UNAVAILABLE,
                                    note="EC2 DescribeInstances — T-series credit mode not in CUR"),
    "cuda_version":        FieldDef("cuda_version",
                                    FieldSource.NOT_AVAILABLE_CUR_ONLY, required=False,
                                    state_if_missing=FieldState.UNAVAILABLE,
                                    note="Runtime config — requires AMI inspection or CloudWatch"),
    "numa_nodes":          FieldDef("numa_nodes",
                                    FieldSource.NOT_AVAILABLE_CUR_ONLY, required=False,
                                    state_if_missing=FieldState.UNAVAILABLE,
                                    note="Hardware topology — not in billing data"),
    "memory_bandwidth_class": FieldDef("memory_bandwidth_class",
                                    FieldSource.NOT_AVAILABLE_CUR_ONLY, required=False,
                                    state_if_missing=FieldState.UNAVAILABLE,
                                    note="Derivable from generation via catalog but not CUR alone"),
    "backup_retention_days": FieldDef("backup_retention_days",
                                    FieldSource.NOT_AVAILABLE_CUR_ONLY, required=False,
                                    state_if_missing=FieldState.UNAVAILABLE,
                                    note="RDS DescribeDBInstances — configuration not in billing"),
    "read_replica_count":  FieldDef("read_replica_count",
                                    FieldSource.NOT_AVAILABLE_CUR_ONLY, required=False,
                                    state_if_missing=FieldState.UNAVAILABLE,
                                    note="Topology — sometimes inferable from CUR line item count"),
    "replication_factor":  FieldDef("replication_factor",
                                    FieldSource.NOT_AVAILABLE_CUR_ONLY, required=False,
                                    state_if_missing=FieldState.UNAVAILABLE,
                                    note="MSK DescribeCluster API"),
    "cluster_mode":        FieldDef("cluster_mode",
                                    FieldSource.NOT_AVAILABLE_CUR_ONLY, required=False,
                                    state_if_missing=FieldState.UNAVAILABLE,
                                    note="ElastiCache DescribeReplicationGroups API"),
}


# ---------------------------------------------------------------------------
# Catalog version stamps  (Phase G: reproducibility)
# ---------------------------------------------------------------------------

@dataclass
class CatalogVersions:
    aws_catalog_version: str   # e.g. "2026-06-30"
    gcp_catalog_version: str
    pricing_catalog_version: str

    def to_dict(self) -> dict:
        return {
            "aws_catalog_version":     self.aws_catalog_version,
            "gcp_catalog_version":     self.gcp_catalog_version,
            "pricing_catalog_version": self.pricing_catalog_version,
        }


# ---------------------------------------------------------------------------
# Field state map builder
# ---------------------------------------------------------------------------

def build_field_state_map(
    profile: dict,
    required_fields: list[str],
) -> dict[str, dict]:
    """
    For each required field, checks whether it's present in the profile and
    annotates its state + source. Fields from NOT_AVAILABLE_CUR_ONLY sources
    always get state=UNAVAILABLE regardless of what the profile contains.

    ram_gb / memory_gb alias: if the requested field is "ram_gb" and it is
    absent from the profile, the lookup also checks "memory_gb" (and vice
    versa). This handles schema drift between ingest.py versions without
    requiring a migration.
    """
    # Build an alias-aware value lookup once up front.
    _RAM_ALIASES = ("ram_gb", "memory_gb")

    def _get_value(fname: str) -> Any:
        val = profile.get(fname)
        if val is None and fname in _RAM_ALIASES:
            # Try the other alias
            for alias in _RAM_ALIASES:
                if alias != fname:
                    val = profile.get(alias)
                    if val is not None:
                        break
        return val

    state_map: dict[str, dict] = {}
    for fname in required_fields:
        fdef = FIELD_REGISTRY.get(fname)
        value = _get_value(fname)

        if fdef and fdef.source == FieldSource.NOT_AVAILABLE_CUR_ONLY:
            state = FieldState.UNAVAILABLE
        elif value is not None:
            state = FieldState.KNOWN
        elif fdef:
            state = fdef.state_if_missing
        else:
            state = FieldState.UNKNOWN

        state_map[fname] = {
            "value":  value,
            "state":  state.value,
            "source": fdef.source.value if fdef else "unknown",
            "note":   fdef.note if fdef else None,
        }
    return state_map


def data_confidence_from_state_map(
    state_map: dict[str, dict],
    required_fields: list[str],
) -> float:
    """
    Returns a 0-1 score reflecting how complete the data is.
    UNKNOWN required fields: heavy penalty (-0.15 each)
    UNAVAILABLE required fields: moderate penalty (-0.08 each, since it's
    not the engine's fault — it just means we'll need to make assumptions)
    KNOWN/DERIVED: no penalty
    """
    if not required_fields:
        return 1.0
    score = 1.0
    for fname in required_fields:
        entry = state_map.get(fname, {})
        s = entry.get("state", "unknown")
        if s == FieldState.UNKNOWN.value:
            score -= 0.15
        elif s == FieldState.UNAVAILABLE.value:
            score -= 0.08
    return max(round(score, 3), 0.0)


# ---------------------------------------------------------------------------
# DB integration: apply field states to projection.duckdb
# ---------------------------------------------------------------------------

def apply_field_states_to_db(db_path: str) -> dict:
    """
    Opens the DuckDB database at db_path (expected: projection.duckdb),
    reads all rows from aws_li_catalog with non-null instance_type, computes
    a field_state_map for each row using all FIELD_REGISTRY keys as the
    required_fields list, and writes the result as a JSON blob into the
    field_states column on aws_li_catalog.

    Returns a summary dict:
        {
            "total_rows": N,
            "rows_with_unavailable_fields": N,
            "unavailable_field_counts": {field_name: count, ...}
        }
    """
    import json
    import duckdb

    con = duckdb.connect(db_path)

    # Add column if it doesn't exist yet
    try:
        con.execute("ALTER TABLE aws_li_catalog ADD COLUMN field_states TEXT")
    except Exception:
        pass  # column already exists

    # Fetch all rows with a non-null instance_type
    rows = con.execute(
        "SELECT * FROM aws_li_catalog WHERE instance_type IS NOT NULL"
    ).fetchall()
    col_names = [desc[0] for desc in con.description]

    all_fields = list(FIELD_REGISTRY.keys())
    total_rows = 0
    rows_with_unavailable = 0
    unavailable_counts: dict[str, int] = {}

    for row in rows:
        profile = dict(zip(col_names, row))
        aws_li_key = profile.get("aws_li_key")

        state_map = build_field_state_map(profile, all_fields)

        # Track unavailable field stats
        has_unavailable = False
        for fname, entry in state_map.items():
            if entry["state"] == FieldState.UNAVAILABLE.value:
                has_unavailable = True
                unavailable_counts[fname] = unavailable_counts.get(fname, 0) + 1

        if has_unavailable:
            rows_with_unavailable += 1

        blob = json.dumps(state_map)
        con.execute(
            "UPDATE aws_li_catalog SET field_states = ? WHERE aws_li_key = ?",
            [blob, aws_li_key],
        )
        total_rows += 1

    con.close()

    return {
        "total_rows": total_rows,
        "rows_with_unavailable_fields": rows_with_unavailable,
        "unavailable_field_counts": unavailable_counts,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: field_state.py <path-to-projection.duckdb>", file=sys.stderr)
        sys.exit(1)

    result = apply_field_states_to_db(sys.argv[1])
    print(json.dumps(result, indent=2))

    if result["rows_with_unavailable_fields"] > 0:
        print(
            f"[WARN] {result['rows_with_unavailable_fields']} rows have fields "
            "unavailable from CUR alone"
        )
