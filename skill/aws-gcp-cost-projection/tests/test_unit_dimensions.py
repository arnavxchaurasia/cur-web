import os
import sys
import duckdb
import pytest

from scripts.unit_equivalence import (
    unit_dims,
    unit_scale,
    compatible,
    conversion_factor,
    normalize_unit,
)
from scripts.resolve_sku import resolve_intent_to_sku, ResolvedSKU


def test_every_catalog_unit_is_known():
    """T5.1: Every usage_unit in data/catalog.duckdb resolves to known dimensions."""
    catalog_path = os.path.join(os.path.dirname(__file__), "..", "data", "catalog.duckdb")
    con = duckdb.connect(catalog_path, read_only=True)
    rows = con.execute("SELECT usage_unit, count(*) FROM skus GROUP BY 1 ORDER BY 2 DESC").fetchall()
    con.close()

    unknown = []
    for u, count in rows:
        dims = unit_dims(u)
        if dims is None:
            unknown.append((u, count))

    assert len(unknown) == 0, f"Found unknown catalog units: {unknown}"


def test_compound_units_are_not_time():
    """T5.2: GiBy.h, GBy.h, GiBy.s, GiBy.d, Lambda-GB-Second, vCPU-Hours have 2-dim vectors and are NOT compatible with Hrs."""
    compound_units = ["GiBy.h", "GBy.h", "GiBy.s", "GiBy.d", "Lambda-GB-Second", "vCPU-Hours"]
    for u in compound_units:
        dims = unit_dims(u)
        assert dims is not None, f"Expected dims for {u}"
        assert sum(dims.values()) >= 2, f"Expected compound dims for {u}, got {dims}"
        ok, why = compatible(u, "Hrs")
        assert not ok, f"Expected {u} to be incompatible with 'Hrs'"


def test_same_dims_different_scale():
    """T5.3: GB-Mo and Lambda-GB-Second have identical dimensions {'data': 1, 'time': 1} and conversion factor ~2.6e6."""
    ok, why = compatible("GB-Mo", "Lambda-GB-Second")
    assert ok, f"GB-Mo and Lambda-GB-Second must be compatible, got {why}"

    factor = conversion_factor("GB-Mo", "Lambda-GB-Second")
    assert factor is not None
    # 679.86548 / 0.0002587007 ≈ 2,627,999.98
    assert 2.5e6 < factor < 2.7e6


def test_unknown_fails_closed():
    """T5.4: compatible('Hrs', 'widgets-per-furlong') returns False, never True."""
    ok, why = compatible("Hrs", "widgets-per-furlong")
    assert not ok
    assert "unknown unit" in why


def test_normalisation():
    """T5.5: GiBy.h, giby-h, GiBy h all reach the same dimensions."""
    d1 = unit_dims("GiBy.h")
    d2 = unit_dims("giby-h")
    d3 = unit_dims("GiBy h")
    assert d1 is not None
    assert d1 == d2 == d3 == {"data": 1, "time": 1}


def test_regression_hrs_vs_giby_h():
    """T5.6: Worked regression test from §1: Hrs vs GiBy.h must be dimensionally incompatible."""
    ok, why = compatible("Hrs", "GiBy.h")
    assert not ok
    assert "dimension mismatch" in why


def test_every_mapper_expected_unit_is_known():
    """Companion to test_every_catalog_unit_is_known: the unit vocabulary the
    MAPPERS emit (resolve_sku.infer_expected_unit outputs + the 'storage-month'
    literal) must all resolve in the dimension table. Otherwise resolve_sku's
    fail-closed check would reject a legitimate mapping -> passthrough with no
    catalog-side signal. Pins the expected-unit vocabulary against drift."""
    from resolve_sku import infer_expected_unit

    mechanic_groups = [
        "compute_breakdown", "compute_windows", "compute_arm", "compute_burstable",
        "flat_hourly", "opensearch", "rds_extended_support", "emr", "mwaa",
        "block_storage", "object_storage", "dynamodb_storage", "efs", "fsx",
        "managed_db", "elasticache", "msk", "redshift",
        "data_transfer", "egress",
        "per_request", "invocations", "athena", "kinesis", "xray", "ssm_parameter_store",
        "quicksight", "seat",
    ]
    components = [None, "core", "ram", "instance", "node", "storage", "other"]

    emitted = {"storage-month"}  # legacy literal still passed by some mappers
    for mg in mechanic_groups:
        for comp in components:
            u = infer_expected_unit(mg, comp)
            if u:
                emitted.add(u)

    unknown = [u for u in sorted(emitted) if unit_dims(u) is None]
    assert not unknown, f"mapper expected-units with no dimension entry: {unknown}"
