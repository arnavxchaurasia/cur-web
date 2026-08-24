"""
Regression guard for a real bug found in production: fix_managed_db_storage_rows()
(apply_rates.py) exists to catch a genuine problem — the LLM sometimes wrongly maps
a GB-Mo storage row to a vCPU/RAM SKU — but it decided whether a row was "really
storage" using a NEGATIVE exclusion on pricing_unit ('Hrs'/'ACU-Hrs'/... excluded).
ingest.py leaves pricing_unit as an empty string ('') for Aurora ServerlessV2Usage
rows rather than 'ACU-Hrs' — '' is neither NULL nor literally excluded, so it slipped
through and silently overwrote a CORRECT Cloud SQL compute mapping with a WRONG
storage one. No LLM was involved anywhere in this chain — it was a plain assumption
one deterministic function made about another's output that nothing ever verified.

This test encodes the actual contract directly: an Aurora Serverless V2 row that
map_managed_db() has already correctly mapped to Cloud SQL compute (vCPU/RAM) must
survive fix_managed_db_storage_rows() untouched, REGARDLESS of what pricing_unit
happens to be — '', 'ACU-Hrs', or anything else. If this ever regresses again (a
future edit narrows the usage_type exclusion, or someone reintroduces a
pricing_unit-only check), this test fails immediately instead of requiring someone
to manually trace a live customer job to notice the report is wrong.
"""
import os
import sys

import duckdb
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import apply_rates as ar  # noqa: E402


def _build_db(tmp_path, pricing_unit):
    db_path = str(tmp_path / "test.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("""
        CREATE TABLE aws_li_catalog (
            aws_li_key VARCHAR, product VARCHAR, usage_type VARCHAR,
            pricing_unit VARCHAR, line_item_type VARCHAR, gcp_region VARCHAR,
            aws_amortized_cost DOUBLE
        )
    """)
    conn.execute("""
        CREATE TABLE aws_li_to_gcp_li (
            aws_li_key VARCHAR, component VARCHAR, gcp_service VARCHAR,
            gcp_sku_name VARCHAR, gcp_sku_id VARCHAR, strategy VARCHAR,
            unit_multiplier DOUBLE, projection_note VARCHAR
        )
    """)
    conn.execute(
        "INSERT INTO aws_li_catalog VALUES (?,?,?,?,?,?,?)",
        ["k1", "Amazon Relational Database Service", "APS5-Aurora:ServerlessV2Usage",
         pricing_unit, "Usage", "asia-south2", 120.0],
    )
    # Simulate map_managed_db()'s correct compute mapping — a vCPU/RAM SKU,
    # which is exactly what fix_managed_db_storage_rows() looks for to "fix".
    conn.execute(
        "INSERT INTO aws_li_to_gcp_li VALUES (?,?,?,?,?,?,?,?)",
        ["k1", "core", "Cloud SQL", "Zonal - 2 vCPU + 4GB RAM", None, "map", 2.0,
         "Aurora Serverless v2 ACU-hrs -> Cloud SQL Zonal - 2 vCPU + 4GB RAM"],
    )
    return conn


@pytest.mark.parametrize("pricing_unit", ["", "ACU-Hrs", "ACU-hours", None])
def test_aurora_serverless_compute_mapping_survives_storage_cleanup(tmp_path, pricing_unit):
    conn = _build_db(tmp_path, pricing_unit)
    fixed_count = ar.fix_managed_db_storage_rows(conn)
    row = conn.execute(
        "SELECT gcp_service, gcp_sku_name, strategy FROM aws_li_to_gcp_li WHERE aws_li_key = 'k1'"
    ).fetchone()
    assert fixed_count == 0, (
        f"fix_managed_db_storage_rows() touched an Aurora Serverless V2 compute row "
        f"(pricing_unit={pricing_unit!r}) — it should NEVER reclassify an ACU-billed "
        f"row as storage, regardless of what pricing_unit ingest.py assigned."
    )
    assert row[1] == "Zonal - 2 vCPU + 4GB RAM", (
        f"Aurora Serverless V2 row was reclassified from compute to {row[1]!r} — "
        f"this is the exact regression that shipped to a real customer report."
    )
    conn.close()
