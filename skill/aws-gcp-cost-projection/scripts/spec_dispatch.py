#!/usr/bin/env python3
"""
spec_dispatch.py — the per-service charge-type dispatch engine (Layer-1.5).

Reads data/service_specs.json and classifies a row by:
    1. service identity (canonical_service, product_code-first)   [Layer 1]
    2. per-service charge_type (first-match-wins WITHIN the service)
    3. -> (mechanic, charge_type, fields, field_state)

This is the replacement engine for the per-service portion of
classify_mechanics.py's flat global rule chain. It owns classification in
the pipeline (OWNS_CLASSIFICATION = True) following WORKORDER_spec_dispatch_cutover.md.

Global, service-agnostic exclusion rules (negative_cost, commitment_discount,
non_workload) are NOT re-implemented here — they stay in classify_mechanics.py
and run as a pre-filter before dispatch. classify_via_specs() wires the two
together as the authoritative classification pipeline.

Pure library + CLI. No DB writes. Import `dispatch` / `classify_via_specs`.
"""
from __future__ import annotations
import os
import re
import sys
import json

OWNS_CLASSIFICATION: bool = True

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import load_data_config as _cfg

# Reuse the exact helpers + exclusion rules from classify_mechanics so the two
# engines can never drift on hourly-unit / accelerator / exclusion semantics.
import classify_mechanics as _cm
from aws_normalizer import canonical_service as _canonical_service

# ---------------------------------------------------------------------------
# Spec registry
# ---------------------------------------------------------------------------
_SPECS_RAW: dict = _cfg("service_specs")
SPECS: dict = {k: v for k, v in _SPECS_RAW.items() if not k.startswith("_")}

# Global exclusion rules (service-agnostic) that run BEFORE per-service dispatch,
# in the same order classify_mechanics applies them. Pulled by name from its
# RULES list so there is a single source of truth for their logic.
_EXCLUSION_ORDER = ["negative_cost", "commitment_discount", "non_workload"]
_EXCLUSION_TESTS = {
    name: test for name, test in _cm.RULES if name in _EXCLUSION_ORDER
}


# ---------------------------------------------------------------------------
# Predicate evaluation
# ---------------------------------------------------------------------------
def _f(row: dict, name: str) -> str:
    return row.get(name) or ""


def _any_text(row: dict) -> str:
    return f"{_f(row, 'usage_type')} {_f(row, 'operation')} {_f(row, 'product')}"


def _eval_block(block: dict, row: dict) -> bool:
    """Return True iff ALL predicates in `block` hold (logical AND).

    An empty block is vacuously True. Unknown predicate keys fail closed
    (return False) so a typo in the spec can never silently widen a match."""
    for key, val in block.items():
        if key == "product_re":
            if not re.search(val, _f(row, "product"), re.IGNORECASE):
                return False
        elif key == "usage_type_re":
            if not re.search(val, _f(row, "usage_type"), re.IGNORECASE):
                return False
        elif key == "operation_re":
            if not re.search(val, _f(row, "operation"), re.IGNORECASE):
                return False
        elif key == "any_field_re":
            if not re.search(val, _any_text(row), re.IGNORECASE):
                return False
        elif key == "exclude_usage_type_re":
            if re.search(val, _f(row, "usage_type"), re.IGNORECASE):
                return False
        elif key == "exclude_operation_re":
            if re.search(val, _f(row, "operation"), re.IGNORECASE):
                return False
        elif key == "exclude_any_field_re":
            # Negative counterpart of any_field_re. Belongs in `match` rather
            # than `exclude_when`, because exclude_when ANDs its predicates —
            # putting a second condition there narrows the exclusion instead of
            # widening it.
            if re.search(val, _any_text(row), re.IGNORECASE):
                return False
        elif key == "product_re_not":
            if re.search(val, _f(row, "product"), re.IGNORECASE):
                return False
        elif key == "unit_in":
            if (row.get("unit") or "").strip().lower() not in [str(x).lower() for x in val]:
                return False
        elif key == "unit_not_in":
            if (row.get("unit") or "").strip().lower() in [str(x).lower() for x in val]:
                return False
        elif key == "unit_re":
            if not re.search(val, row.get("unit") or "", re.IGNORECASE):
                return False
        elif key == "unit_hourly":
            if _cm._is_hourly_unit(row.get("unit")) != bool(val):
                return False
        elif key == "pricing_model_in":
            if row.get("pricing_model") not in val:
                return False
        elif key == "line_item_type_in":
            if row.get("line_item_type") not in val:
                return False
        elif key == "cost_lt_zero":
            if ((row.get("aws_amortized_cost") or 0) < 0) != bool(val):
                return False
        elif key == "is_accelerator":
            if _cm._is_accelerator(row) != bool(val):
                return False
        else:
            # Unknown predicate — fail closed and surface it.
            print(f"spec_dispatch: WARNING unknown predicate '{key}' — failing match closed",
                  file=sys.stderr)
            return False
    return True


def _charge_type_matches(ct: dict, row: dict) -> bool:
    # exclude_when short-circuits the whole charge_type.
    exclude_when = ct.get("exclude_when")
    if exclude_when and _eval_block(exclude_when, row):
        return False
    # Base = match (AND of its predicates; absent => vacuously True).
    base = _eval_block(ct.get("match", {}), row)
    # any_of = OR of sub-blocks; when present it must also hold.
    if "any_of" in ct:
        base = base and any(_eval_block(b, row) for b in ct["any_of"])
    if base:
        return True
    fb = ct.get("fallback_match")
    if fb and _eval_block(fb, row):
        return True
    lit_list = ct.get("also_match_line_item_type_in")
    if lit_list and row.get("line_item_type") in lit_list:
        return True
    return False


# ---------------------------------------------------------------------------
# Field state (completeness/provenance preview)
# ---------------------------------------------------------------------------
# Map a field's declared cur_column to the row key ingest.py actually populates,
# so we can report whether the value is really present for this row.
_CUR_COLUMN_TO_ROW_KEY = {
    "product/instanceType": "instance_type",
    "product/vCPU": "instance_vcpus",
    "product/memory": "instance_ram_gb",
    "lineItem/UsageType": "usage_type",
    "lineItem/UsageAmount": "total_usage",
    "lineItem/Operation": "operation",
    "product/region": "aws_region",
    "pricing/term": "pricing_model",
    "product/operatingSystem": "operating_system",
    "product/licenseModel": "license_model",
    "product/databaseEngine": "database_engine",
    "product/deploymentOption": "deployment_option",
    "product/volumeType": "volume_type",
}


def _field_state(ct: dict, row: dict) -> list[dict]:
    out = []
    for fld in ct.get("fields", []):
        cur_col = fld.get("cur_column")
        source = fld.get("source")
        if cur_col is None:
            status = "not_in_cur"           # AWS never bills it; must enrich
        elif source == "cur":
            row_key = _CUR_COLUMN_TO_ROW_KEY.get(cur_col)
            present = bool(row.get(row_key)) if row_key else False
            status = "present_in_cur" if present else "missing_in_cur"
        elif source == "table":
            status = "derive_from_table"
        elif source == "api":
            status = "needs_api"
        elif source == "llm":
            status = "needs_llm"
        else:
            status = "unknown_source"
        out.append({"name": fld["name"], "source": source,
                    "cur_column": cur_col, "required": fld.get("required", False),
                    "status": status})
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def is_default_charge_type(ct: dict) -> bool:
    """A charge_type with no positive predicates — the declared catch-all for
    its level. It matches anything that reached it, so overlapping with it is
    by design, not ambiguity. `exclude_when` is still allowed (a guarded
    default)."""
    return not (ct.get("match") or ct.get("any_of") or ct.get("fallback_match")
                or ct.get("also_match_line_item_type_in"))


def dispatch(row: dict) -> dict | None:
    """Per-service dispatch. Returns a result dict, or None when the service
    has no spec (caller should fall back to the legacy classifier).

    Charge types may nest: a charge_type carrying "children" is a GROUP. Its own
    `match` answers the coarse question ("is this an instance-hour row?") and its
    children answer the fine one ("Windows? burstable? Graviton? plain?").

    Why nesting rather than a flat list: measured on 12 real bills, 335 rows
    ($53k) matched two or three sibling charge types at once — every one of them
    a specific variant plus the general case written as flat siblings
    (instance_hour_graviton + instance_hour, etc.). Flat ordering resolved those
    correctly only by list position. Under a group, a child cannot collide with
    its parent, and siblings are required to partition the parent's rows — an
    invariant tests/test_spec_partition.py enforces. See
    docs/ANALYSIS_spec_dispatch_validation.md.

    The last child of a group must be a default (no positive predicates), so a
    row entering a group always leaves it with a named charge type.
    """
    canon = row.get("canonical")
    if canon is None:
        canon = _canonical_service(row.get("product"), row.get("product_code"))
    spec = SPECS.get(canon)
    if not spec:
        return None

    def _result(ct, group=None):
        return {
            "service": canon,
            "charge_type": ct["id"],
            "group": group,
            "mechanic": ct["mechanic"],
            "fields": ct.get("fields", []),
            "field_state": _field_state(ct, row),
        }

    for ct in spec.get("charge_types", []):
        if not _charge_type_matches(ct, row):
            continue
        children = ct.get("children")
        if not children:
            return _result(ct)
        for child in children:
            if _charge_type_matches(child, row):
                # A child may itself be a group (depth is not limited).
                grandchildren = child.get("children")
                if grandchildren:
                    for gc in grandchildren:
                        if _charge_type_matches(gc, row):
                            return _result(gc, group=child["id"])
                return _result(child, group=ct["id"])
        # No child matched. The spec is incomplete at this level — the group
        # should declare a default child. Fall back to the group's own mechanic
        # when it has one so a row is never dropped over a spec gap.
        if ct.get("mechanic"):
            return _result(ct)
        return None
    return None


def matching_charge_types(canon: str, row: dict) -> list[str]:
    """Every charge_type id in `canon`'s spec that matches `row`, at every
    level. Used by the ambiguity tests and scripts/spec_parity.py — dispatch()
    itself only ever returns the first match."""
    spec = SPECS.get(canon) or {}
    out = []

    def walk(cts, prefix=""):
        for ct in cts:
            if _charge_type_matches(ct, row):
                name = prefix + ct["id"]
                kids = ct.get("children")
                if kids:
                    before = len(out)
                    walk(kids, name + ".")
                    if len(out) == before:
                        out.append(name)
                else:
                    out.append(name)

    walk(spec.get("charge_types", []))
    return out


SPEC_GAP_COUNTS: dict[str, int] = {}


def record_spec_gap(service: str, row: dict | None = None) -> None:
    SPEC_GAP_COUNTS[service] = SPEC_GAP_COUNTS.get(service, 0) + 1


def classify_via_specs_with_trace(row: dict) -> tuple[str, str]:
    """Authoritative classification path:
    1. Global exclusion pre-filters (negative_cost, commitment_discount, non_workload)
    2. Per-service spec dispatch (Layer 1.5)
    3. If service has a spec but no charge_type matched -> explicit generic fallback ("misc", "spec_gap:<svc>")
    4. If service has no spec -> demoted long-tail fallback rules from classify_mechanics
    Guaranteed to return a non-null mechanic_group.
    """
    for name in _EXCLUSION_ORDER:
        test = _EXCLUSION_TESTS.get(name)
        if test is not None:
            try:
                if test(row):
                    return name, f"prefilter:{name}"
            except Exception:
                pass

    d = dispatch(row)
    if d is not None:
        return d["mechanic"], f"spec:{d['service']}.{d['charge_type']}"

    canon = row.get("canonical")
    if canon is None:
        canon = _canonical_service(row.get("product"), row.get("product_code"))

    if canon in SPECS:
        # Service has a declared spec, but no charge-type matched (spec gap).
        # Route to explicit generic fallback; loud, not silent.
        record_spec_gap(canon, row)
        return "misc", f"spec_gap:{canon}"

    # Unspecced service: demoted long-tail rules in classify_mechanics
    if hasattr(_cm, "classify_long_tail_with_trace"):
        group, rule = _cm.classify_long_tail_with_trace(row)
    elif hasattr(_cm, "_legacy_classify_with_trace"):
        group, rule = _cm._legacy_classify_with_trace(row)
    elif hasattr(_cm, "classify_with_trace"):
        group, rule = _cm.classify_with_trace(row)
    else:
        group = _cm.classify(row)
        rule = "legacy"

    return group or "misc", rule or "misc"


def classify_via_specs(row: dict) -> str:
    """Full authoritative classification path."""
    return classify_via_specs_with_trace(row)[0]


# ---------------------------------------------------------------------------
# CLI: explain a single synthetic row, or self-check the registry loads.
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    print(f"spec_dispatch: loaded {len(SPECS)} service spec(s): {sorted(SPECS)}")
    for svc, spec in SPECS.items():
        ids = [c["id"] for c in spec.get("charge_types", [])]
        print(f"  {svc:<8} charge_types: {ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
