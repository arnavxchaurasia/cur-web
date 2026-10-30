#!/usr/bin/env python3
"""
resolve_sku.py — Deterministic GCP SKU intent-to-ID resolver for Phase 2 mappings.

Guardrails G-1, G-1b, B, C-1:
- The LLM must NOT fabricate raw `gcp_sku_id` strings.
- Mappings provide intent in words: (gcp_service, gcp_sku_name, gcp_region, expected_unit).
- Opens catalog.duckdb ONCE and reuses the connection across all calls (B).
- Validates the resolved SKU's billing unit against expected_unit using unit equivalence
  classes from unit_equivalence.py; rejects dimensional mismatches as passthrough (G-1b).
"""

from __future__ import annotations

import os
import sys
from collections import namedtuple
import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from synthetic_skus import SYNTHETIC_SKU_IDS
from unit_equivalence import unit_class, compatible, unit_dims

try:
    from diagnostics import diag, WARN
except Exception:
    def diag(*args, **kwargs): pass
    WARN = "warn"

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(_SCRIPTS_DIR))
_DATA_DIR = os.path.join(_SKILL_DIR, "data")
CATALOG_DB = os.path.join(_DATA_DIR, "catalog.duckdb")

# Cache to avoid re-opening the database or re-querying sets repeatedly during a run (B)
_CATALOG_CONN: duckdb.DuckDBPyConnection | None = None
_SERVICES_CACHE: set[str] | None = None
_VALID_SKUS_CACHE: set[str] | None = None


class ResolvedSKU(namedtuple("ResolvedSKU", ["sku_id", "unit", "note"])):
    """Immutable result of SKU intent resolution."""
    __slots__ = ()

    def __bool__(self) -> bool:
        return self.sku_id is not None


def get_catalog_connection() -> duckdb.DuckDBPyConnection | None:
    """Return a cached read-only DuckDB connection to catalog.duckdb, or None if unavailable (B)."""
    global _CATALOG_CONN
    if _CATALOG_CONN is not None:
        try:
            _CATALOG_CONN.execute("SELECT 1")
            return _CATALOG_CONN
        except Exception:
            _CATALOG_CONN = None

    if not os.path.exists(CATALOG_DB):
        return None
    try:
        _CATALOG_CONN = duckdb.connect(CATALOG_DB, read_only=True)
        return _CATALOG_CONN
    except Exception:
        return None


def close_catalog_connection() -> None:
    """Close the cached catalog connection if open."""
    global _CATALOG_CONN
    if _CATALOG_CONN is not None:
        try:
            _CATALOG_CONN.close()
        except Exception:
            pass
        _CATALOG_CONN = None


def get_valid_catalog_services() -> set[str]:
    """Return the set of valid GCP service names from catalog.duckdb."""
    global _SERVICES_CACHE
    if _SERVICES_CACHE is not None:
        return _SERVICES_CACHE

    conn = get_catalog_connection()
    if conn is None:
        return set()

    try:
        rows = conn.execute("SELECT DISTINCT service_name FROM skus WHERE service_name IS NOT NULL").fetchall()
        _SERVICES_CACHE = {r[0] for r in rows if r[0]}
        _SERVICES_CACHE.update({
            "Compute Engine",
            "Cloud Storage",
            "Cloud SQL",
            "Kubernetes Engine",
            "Cloud Logging",
            "Cloud Monitoring",
            "Cloud Run Functions",
            "Cloud Run",
            "Cloud Pub/Sub",
            "Looker Studio Pro",
            "Cloud TPU",
            "Networking",
            "Cloud Load Balancing",
            "Cloud DNS",
            "Cloud NAT",
            "Cloud VPN",
            "Cloud Interconnect",
            "Cloud Armor",
            "Manual Review",
            "Manual Sizing Required",
        })
        return _SERVICES_CACHE
    except Exception:
        return set()


def get_valid_catalog_skus() -> set[str]:
    """Return all valid SKU IDs in catalog.duckdb that have a positive rate in tiered_rates."""
    global _VALID_SKUS_CACHE
    if _VALID_SKUS_CACHE is not None:
        return _VALID_SKUS_CACHE

    conn = get_catalog_connection()
    if conn is None:
        return set()

    try:
        tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
        if "tiered_rates" in tables:
            rows = conn.execute("""
                SELECT DISTINCT s.sku_id 
                FROM skus s
                JOIN tiered_rates t ON t.sku_id = s.sku_id
                WHERE s.sku_id IS NOT NULL AND t.rate_usd > 0
            """).fetchall()
        else:
            rows = conn.execute("SELECT DISTINCT sku_id FROM skus WHERE sku_id IS NOT NULL").fetchall()
        _VALID_SKUS_CACHE = {r[0] for r in rows if r[0]}
        return _VALID_SKUS_CACHE
    except Exception:
        return set()



_STOP_WORDS = {
    "the", "a", "an", "in", "on", "of", "for", "to", "and", "or", "by", "at",
    "running", "google", "cloud",
}


def _score_sku_match(description: str, gcp_sku_name: str) -> int:
    """Score how well a catalog SKU description matches intent."""
    desc_words = set(description.lower().split()) - _STOP_WORDS
    name_words = set(gcp_sku_name.lower().split()) - _STOP_WORDS
    if not name_words:
        return 0
    overlap = len(desc_words & name_words)
    min_required = 2 if len(name_words) >= 3 else 1
    if overlap < min_required:
        return 0
    return overlap


# Mechanic groups intentionally unverified with rationale (RV-3):
# - commitment_discount, negative_cost: non-workload / ignored ($0)
# - non_workload: non-workload passthrough
# - shield, guardduty, inspector: security fixed/event bundles or passthrough
# - marketplace_thirdparty: SaaS passthrough
# - glue, cloudwatch: composite/multi-dimensional meters
# - misc: unclassified/catch-all, handled by LLM/heuristics
_UNVERIFIED_MECHANIC_GROUPS: dict[str, str] = {
    "commitment_discount": "ignored billing mechanic",
    "negative_cost": "credit/refund billing mechanic",
    "non_workload": "non-workload passthrough",
    "shield": "security subscription fee",
    "guardduty": "event/analysis passthrough",
    "inspector": "assessment passthrough",
    "marketplace_thirdparty": "third-party marketplace passthrough",
    "glue": "multi-metric ETL",
    "cloudwatch": "multi-dimensional monitoring metrics",
    "misc": "unclassified fallback",
}


def infer_expected_unit(
    mechanic_group: str | None = None,
    component: str | None = None,
) -> str | None:
    """Infer authoritative expected billing unit from mechanic group / component (RV-3).
    
    Covers all standard workload mechanic groups:
    - time: compute_breakdown, compute_windows, compute_arm, compute_burstable, flat_hourly, opensearch, rds_extended_support, emr, mwaa
    - storage-month: block_storage, object_storage, dynamodb_storage, efs, fsx
    - managed_db, elasticache, msk: time (for core/ram/instance/node), storage-month (for storage/other)
    - data: data_transfer, egress
    - count: per_request, invocations, athena, kinesis, xray, ssm_parameter_store
    - seat: quicksight
    """
    if not mechanic_group:
        return None
    mg = str(mechanic_group).strip().lower()

    if mg in (
        "compute_breakdown", "compute_windows", "compute_arm", "compute_burstable",
        "flat_hourly", "opensearch", "rds_extended_support", "emr", "mwaa",
    ):
        return "hour"

    if mg in ("block_storage", "object_storage", "dynamodb_storage", "efs", "fsx"):
        return "GiBy.mo"

    if mg in ("managed_db", "elasticache", "msk", "redshift"):
        comp = (component or "").strip().lower()
        if comp in ("core", "ram", "instance", "node"):
            return "hour"
        return "GiBy.mo"

    if mg in ("data_transfer", "egress"):
        return "gibibyte"

    if mg in ("per_request", "invocations", "athena", "kinesis", "xray", "ssm_parameter_store"):
        return "count"

    if mg in ("quicksight", "seat"):
        return "user"

    return None


def resolve_intent_to_sku(
    gcp_service: str,
    gcp_sku_name: str,
    gcp_region: str | None = None,
    expected_unit: str | None = None,
    mechanic_group: str | None = None,
    component: str | None = None,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> ResolvedSKU:
    """Resolve intent (service + sku_name + region) to ResolvedSKU(sku_id, usage_unit, note).

    Performs G-1b unit validation against expected_unit:
    - Same unit class: accepted.
    - Different known unit classes: REJECTED -> ResolvedSKU(None, None, '[unit-mismatch: ...]').
    - Unknown unit class: accepted but stamped with '[unit-unverified: ...]'.
    - No matching SKU: ResolvedSKU(None, None, '[no-catalog-sku: ...]').
    """
    if not gcp_service or not gcp_sku_name:
        return ResolvedSKU(None, None, "missing_intent_fields")

    active_conn = conn or get_catalog_connection()
    if active_conn is None:
        return ResolvedSKU(None, None, "catalog_unavailable")

    service_aliases = {
        "Cloud Run Functions": "Cloud Functions",
        "Cloud Pub/Sub": "Cloud Pub/Sub",
        "Cloud Load Balancing": "Networking",
        "Cloud DNS": "Networking",
        "Cloud NAT": "Networking",
        "Cloud VPN": "Networking",
        "Cloud Armor": "Networking",
    }
    svc_lookup = service_aliases.get(gcp_service, gcp_service)

    try:
        tables = {r[0] for r in active_conn.execute("SHOW TABLES").fetchall()}
        has_tiered = "tiered_rates" in tables
        if has_tiered:
            rows = active_conn.execute("""
                SELECT s.sku_id, s.description, s.usage_unit, s.service_regions
                FROM skus s
                JOIN tiered_rates t ON t.sku_id = s.sku_id
                WHERE (s.service_name = ? OR s.service_name = ?)
                  AND s.usage_type = 'OnDemand'
                  AND t.rate_usd > 0
            """, [svc_lookup, gcp_service]).fetchall()
        else:
            rows = active_conn.execute("""
                SELECT s.sku_id, s.description, s.usage_unit, s.service_regions
                FROM skus s
                WHERE (s.service_name = ? OR s.service_name = ?)
                  AND s.usage_type = 'OnDemand'
            """, [svc_lookup, gcp_service]).fetchall()

        if not rows:
            return ResolvedSKU(None, None, f"[no-catalog-sku: service '{gcp_service}' has no priced OnDemand SKUs]")


        best_exact_score, best_exact_res = (-1, 0), None
        best_any_score, best_any_res = (-1, 0), None

        for sku, desc, unit, regions in rows:
            dl = desc.lower()
            if "free tier" in dl or "promotional" in dl or "trial" in dl:
                continue
            if "intra zone" in dl or "intra-zone" in dl or "intra region" in dl or "intra-region" in dl:
                continue

            score = _score_sku_match(desc, gcp_sku_name)
            if score <= 0:
                continue

            rank = (score, -len(desc.split()))
            # RV-minor: Guard against string representation in service_regions (prevent substring false match)
            if isinstance(regions, str):
                reg_list = [r.strip() for r in re.split(r'[,\s]+', regions.strip("[]'\"")) if r.strip()]
            elif isinstance(regions, (list, tuple, set)):
                reg_list = list(regions)
            else:
                reg_list = []

            if gcp_region and (gcp_region in reg_list or "global" in reg_list):
                if rank > best_exact_score:
                    best_exact_score, best_exact_res = rank, (sku, unit)

            if rank > best_any_score:
                best_any_score, best_any_res = rank, (sku, unit)

        if best_exact_res is not None and best_any_res is not None:
            best_match = best_exact_res if best_exact_score >= best_any_score else best_any_res
        else:
            best_match = best_exact_res or best_any_res

        if not best_match:
            return ResolvedSKU(None, None, f"[no-catalog-sku: no catalog SKU for '{gcp_sku_name}']")

        resolved_sku_id, resolved_unit = best_match

        # G-1b & RV-2/RV-3: Authoritative expected unit validation
        # The authoritative expected unit is the mechanic/charge-type, NOT the LLM's own gcp_sku_unit
        mech_unit = infer_expected_unit(mechanic_group, component)
        used_intent_fallback = False
        if mech_unit:
            exp_unit = mech_unit
        elif expected_unit:
            exp_unit = expected_unit
            used_intent_fallback = True
        else:
            exp_unit = None

        if exp_unit:
            ok, why = compatible(resolved_unit, exp_unit)
            if not ok:
                code = "unit_unknown" if "unknown unit" in why else "unit_mismatch"
                diag(
                    WARN,
                    code,
                    "resolve_sku.py",
                    f"Unit rejection: {why} (resolved SKU={resolved_sku_id} unit={resolved_unit}, expected={exp_unit})",
                    detail={"resolved_sku": resolved_sku_id, "resolved_unit": resolved_unit, "expected_unit": exp_unit, "why": why},
                )
                # Dimensional mismatch or unknown unit: REJECT (G-1b)
                return ResolvedSKU(
                    None,
                    None,
                    f"[unit-mismatch: {why} — passthrough]",
                )
            # Compatible dimensions -> accept
            return ResolvedSKU(resolved_sku_id, resolved_unit, None)

        return ResolvedSKU(resolved_sku_id, resolved_unit, None)

    except Exception as e:
        return ResolvedSKU(None, None, f"[resolver-error: {e}]")
