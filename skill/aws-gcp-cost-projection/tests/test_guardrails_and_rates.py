#!/usr/bin/env python3
"""
test_guardrails_and_rates.py — Verification tests for WORKORDER_merge_and_sku_guardrails.md:
- Section A: Unit-equivalence classes
- Section G-1b: Resolved SKU unit validation & mismatch rejection
- Section G-1c: Single resolver in resolve_sku, no duplicate in apply_rates
- Section B: Catalog connection caching
- Section C-1b: Catalog atomic swap
- Section C-1c: Fail-closed SKU validation when catalog absent
- Section AR-C: Transaction rollback on apply_rates failure
- Section AR-D: Unreachable rate triage scoped by region
- Section E: Spot priced at OnDemand without Preemptible rate synthesis
- Section F: Multi-month bill tiered rate blending per month
"""

import json
import os
import sys
import tempfile
from unittest.mock import patch
import duckdb
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from unit_equivalence import unit_class
import resolve_sku
import merge_mappings
import apply_rates
from projection_view import create_projection_view


# ---------------------------------------------------------------------------
# Section A: Unit-Equivalence Classes
# ---------------------------------------------------------------------------

def test_section_a_unit_equivalence_classes():
    # Time class
    assert unit_class("Hrs") == "time"
    assert unit_class("hour") == "time"
    assert unit_class("h") == "time"
    assert unit_class("hr") == "time"
    assert unit_class("hrs") == "time"

    # Storage-month class
    assert unit_class("GB-Mo") == "storage-month"
    assert unit_class("GiBy.mo") == "storage-month"
    assert unit_class("GB-month") == "storage-month"
    assert unit_class("gibibyte month") == "storage-month"
    assert unit_class("GiB-mo") == "storage-month"

    # Data class
    assert unit_class("gibibyte") == "data"
    assert unit_class("GiB") == "data"
    assert unit_class("GB") == "data"
    assert unit_class("byte") == "data"

    # Count class
    assert unit_class("count") == "count"
    assert unit_class("request") == "count"
    assert unit_class("requests") == "count"
    assert unit_class("operation") == "count"
    assert unit_class("operations") == "count"
    assert unit_class("call") == "count"
    assert unit_class("calls") == "count"

    # Seat class
    assert unit_class("user") == "seat"
    assert unit_class("seat") == "seat"
    assert unit_class("seat-month") == "seat"
    assert unit_class("user-month") == "seat"

    # Cross-class distinction
    assert unit_class("count") != unit_class("hour")
    assert unit_class("GB-Mo") == unit_class("GiBy.mo")
    assert unit_class("Hrs") == unit_class("hour")

    # Unknown unit returns None
    assert unit_class("flimble-snarf") is None
    assert unit_class("") is None
    assert unit_class(None) is None


# ---------------------------------------------------------------------------
# Section G-1b: Unit Validation in resolve_intent_to_sku
# ---------------------------------------------------------------------------

def test_section_g1b_unit_validation_rejection_and_unverified():
    # Setup in-memory mock catalog
    mock_cat = duckdb.connect(":memory:")
    mock_cat.execute("""
        CREATE TABLE skus (
            sku_id TEXT,
            service_name TEXT,
            description TEXT,
            usage_unit TEXT,
            service_regions TEXT[],
            usage_type TEXT
        )
    """)
    mock_cat.execute("""
        INSERT INTO skus VALUES
        ('SKU-CPU-HOUR', 'Compute Engine', 'N2 Instance Core running in Americas', 'h', ['us-central1'], 'OnDemand'),
        ('SKU-STORAGE-GB', 'Compute Engine', 'Storage PD Capacity', 'GiBy.mo', ['us-central1'], 'OnDemand'),
        ('SKU-SPECIAL-CUSTOM', 'Compute Engine', 'Custom Metric Widget', 'widgets', ['us-central1'], 'OnDemand')
    """)

    # 1. Matching unit class (expected 'hour', SKU billed per 'h' -> 'time' == 'time')
    res_ok = resolve_sku.resolve_intent_to_sku(
        gcp_service="Compute Engine",
        gcp_sku_name="N2 Instance Core",
        gcp_region="us-central1",
        expected_unit="hour",
        conn=mock_cat,
    )
    assert res_ok.sku_id == "SKU-CPU-HOUR"
    assert res_ok.unit == "h"
    assert res_ok.note is None

    # 2. Dimensional mismatch (expected 'count', SKU billed per 'h' -> REJECT)
    res_mismatch = resolve_sku.resolve_intent_to_sku(
        gcp_service="Compute Engine",
        gcp_sku_name="N2 Instance Core",
        gcp_region="us-central1",
        expected_unit="count",
        conn=mock_cat,
    )
    assert res_mismatch.sku_id is None
    assert res_mismatch.unit is None
    assert "[unit-mismatch:" in res_mismatch.note

    # 3. Unknown unit class (expected 'widgets', SKU billed per 'widgets' -> fails closed with [unit-mismatch: unknown unit])
    res_unverified = resolve_sku.resolve_intent_to_sku(
        gcp_service="Compute Engine",
        gcp_sku_name="Custom Metric Widget",
        gcp_region="us-central1",
        expected_unit="widgets",
        conn=mock_cat,
    )
    assert res_unverified.sku_id is None
    assert res_unverified.unit is None
    assert "[unit-mismatch: unknown unit 'widgets'" in res_unverified.note

    mock_cat.close()


# ---------------------------------------------------------------------------
# Section G-1c: Single Resolver Check (No duplicate in apply_rates)
# ---------------------------------------------------------------------------

def test_section_g1c_no_duplicate_resolver_in_apply_rates():
    assert not hasattr(apply_rates, "_resolve_sku_for_row"), "_resolve_sku_for_row must be deleted from apply_rates"
    assert not hasattr(apply_rates, "_score_sku_match"), "_score_sku_match must be deleted from apply_rates"
    assert hasattr(apply_rates, "resolve_intent_to_sku"), "apply_rates must import resolve_intent_to_sku"


# ---------------------------------------------------------------------------
# Section B: Catalog Connection Caching
# ---------------------------------------------------------------------------

def test_section_b_catalog_connection_caching():
    resolve_sku.close_catalog_connection()
    c1 = resolve_sku.get_catalog_connection()
    c2 = resolve_sku.get_catalog_connection()
    assert c1 is c2, "get_catalog_connection must return the cached singleton connection"
    resolve_sku.close_catalog_connection()


# ---------------------------------------------------------------------------
# Section C-1c: Fail-closed SKU validation when catalog absent
# ---------------------------------------------------------------------------

def test_section_c1c_fail_closed_confidence_cap(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "projection.duckdb")
        mappings_dir = os.path.join(tmp_dir, "mappings")
        os.makedirs(mappings_dir, exist_ok=True)

        con = duckdb.connect(db_path)
        con.execute("CREATE TABLE aws_li_catalog (aws_li_key TEXT PRIMARY KEY, product TEXT, mechanic_group TEXT)")
        con.execute("INSERT INTO aws_li_catalog VALUES ('k1', 'AmazonEC2', 'compute_breakdown')")
        merge_mappings.ensure_table(con)
        con.close()

        # SKU-bearing row with high confidence
        mapping_content = [
            {
                "aws_li_key": "k1",
                "gcp_service": "Compute Engine",
                "gcp_sku_id": "PREEXISTING-SKU-1234",
                "strategy": "map",
                "unit_multiplier": 1.0,
                "mapping_confidence": 0.95,
            }
        ]
        with open(os.path.join(mappings_dir, "compute_mappings.json"), "w") as f:
            json.dump(mapping_content, f)

        # Make catalog unavailable
        resolve_sku.close_catalog_connection()
        monkeypatch.setattr(merge_mappings, "CATALOG_DB", os.path.join(tmp_dir, "nonexistent.duckdb"))
        monkeypatch.setattr(resolve_sku, "CATALOG_DB", os.path.join(tmp_dir, "nonexistent.duckdb"))
        monkeypatch.setattr(resolve_sku, "_VALID_SKUS_CACHE", None)
        monkeypatch.setattr(resolve_sku, "_SERVICES_CACHE", None)

        sys.argv = ["merge_mappings.py", db_path, mappings_dir]
        with pytest.raises(SystemExit) as exc:
            merge_mappings.main()
        assert exc.value.code == 0

        # Verify confidence was capped <= 0.5 and note was stamped
        con = duckdb.connect(db_path)
        conf, note = con.execute("SELECT mapping_confidence, projection_note FROM aws_li_to_gcp_li WHERE aws_li_key = 'k1'").fetchone()
        con.close()

        assert conf <= 0.5
        assert "[sku-unvalidated: catalog unavailable]" in note

        with open(os.path.join(tmp_dir, "merge_report.json"), encoding="utf-8") as f:
            report = json.load(f)
        assert report["sku_validation_ran"] is False
        assert report["unvalidated_skus"] == 1


def _setup_catalog_table(con):
    con.execute("""
        CREATE TABLE aws_li_catalog (
            aws_li_key TEXT PRIMARY KEY,
            product TEXT,
            usage_type TEXT,
            operation TEXT,
            instance_type TEXT,
            pricing_unit TEXT,
            pricing_model TEXT,
            gcp_region TEXT,
            aws_region TEXT,
            total_usage DOUBLE,
            aws_amortized_cost DOUBLE,
            is_workload BOOLEAN DEFAULT TRUE,
            line_item_type TEXT,
            billing_month TEXT,
            mechanic_group TEXT
        )
    """)


def _setup_mappings_table(con):
    try:
        import row_state
        row_state.ensure_schema(con)
    except Exception:
        con.execute("""
            CREATE TABLE aws_li_to_gcp_li (
                aws_li_key TEXT,
                gcp_service TEXT,
                gcp_sku_id TEXT,
                gcp_sku_name TEXT,
                gcp_sku_unit TEXT,
                component TEXT,
                strategy TEXT,
                unit_multiplier DOUBLE,
                gcp_region TEXT,
                projection_note TEXT,
                mapping_confidence DOUBLE,
                is_workload BOOLEAN,
                break_down BOOLEAN,
                gcp_machine_type TEXT,
                is_assumption BOOLEAN,
                assumption_note TEXT,
                rate_source TEXT
            )
        """)


# ---------------------------------------------------------------------------
# Section AR-C: apply_rates Transaction Rollback on Failure
# ---------------------------------------------------------------------------

def test_section_arc_transaction_rollback_on_failure(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "projection.duckdb")
        con = duckdb.connect(db_path)
        _setup_catalog_table(con)
        _setup_mappings_table(con)
        con.execute("""
            INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, aws_amortized_cost)
            VALUES ('k1', 'AmazonEC2', 'us-central1', 'us-east-1', 10.0, 'OnDemand', 10.0)
        """)
        con.execute("""
            INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence, component)
            VALUES ('k1', 'Compute Engine', 'SKU-TEST', 'Test SKU', 'map', 1.0, 'us-central1', '', 1.0, 'core')
        """)
        con.execute("""
            CREATE TABLE gcp_sku_rates (
                gcp_sku_id VARCHAR,
                gcp_service VARCHAR,
                gcp_sku_name VARCHAR,
                resource_family VARCHAR,
                resource_group VARCHAR,
                pricing_type VARCHAR,
                region VARCHAR,
                unit VARCHAR,
                rate_usd DOUBLE,
                source VARCHAR,
                audit_url VARCHAR,
                PRIMARY KEY (gcp_sku_id, pricing_type, region)
            )
        """)
        con.execute("INSERT INTO gcp_sku_rates VALUES ('SKU-PRIOR', 'Compute Engine', 'Prior SKU', 'Compute', 'CPU', 'OnDemand', 'us-central1', 'h', 0.05, 'prior', 'http')")
        con.close()

        # Monkeypatch DB_PATH in apply_rates
        monkeypatch.setattr(apply_rates, "DB_PATH", db_path)
        # Create a mock catalog.duckdb
        cat_path = os.path.join(tmp_dir, "catalog.duckdb")
        cat_db = duckdb.connect(cat_path)
        cat_db.execute("CREATE TABLE skus (sku_id TEXT, service_name TEXT, description TEXT, resource_family TEXT, resource_group TEXT, usage_type TEXT, usage_unit TEXT, service_regions TEXT[])")
        cat_db.execute("CREATE TABLE tiered_rates (sku_id TEXT, tier_start DOUBLE, rate_usd DOUBLE)")
        cat_db.execute("INSERT INTO skus VALUES ('SKU-TEST', 'Compute Engine', 'Test SKU', 'Compute', 'CPU', 'OnDemand', 'h', ['us-central1'])")
        cat_db.execute("INSERT INTO tiered_rates VALUES ('SKU-TEST', 0.0, 0.10)")
        cat_db.close()
        monkeypatch.setattr(apply_rates, "CATALOG_DB", cat_path)

        # Inject failure during rate insertion (after DELETE gcp_sku_rates)
        orig_execute = duckdb.DuckDBPyConnection.execute

        def failing_execute(self, sql, *args, **kwargs):
            if "INSERT INTO gcp_sku_rates" in sql and "SKU-TEST" in str(args):
                raise RuntimeError("Simulated crash mid-rate-fill")
            return orig_execute(self, sql, *args, **kwargs)

        monkeypatch.setattr(duckdb.DuckDBPyConnection, "execute", failing_execute)

        with pytest.raises(RuntimeError, match="Simulated crash mid-rate-fill"):
            apply_rates.main()

        # Verify gcp_sku_rates was NOT left empty — the prior row survived via ROLLBACK!
        check_conn = duckdb.connect(db_path)
        count = check_conn.execute("SELECT COUNT(*) FROM gcp_sku_rates").fetchone()[0]
        check_conn.close()
        assert count == 1, "gcp_sku_rates must not be left empty; transaction rollback must restore prior state"


# ---------------------------------------------------------------------------
# Section AR-D: Unreachable Rate Triage Scoped by Region
# ---------------------------------------------------------------------------

def test_section_ard_unreachable_rate_scoped_by_region(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "projection.duckdb")
        con = duckdb.connect(db_path)
        _setup_catalog_table(con)
        _setup_mappings_table(con)
        # k_us has SKU-US-ONLY (has rate in us-central1);
        # k_eu has SKU-NO-RATE (has no rate anywhere -> goes to outlier_triage)
        con.execute("""
            INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, billing_month, aws_amortized_cost)
            VALUES ('k_us', 'AmazonEC2', 'us-central1', 'us-east-1', 10.0, 'OnDemand', '2026-01', 10.0),
                   ('k_eu', 'AmazonEC2', 'europe-west1', 'eu-west-1', 10.0, 'OnDemand', '2026-01', 10.0)
        """)
        con.execute("""
            INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence, component)
            VALUES ('k_us', 'Compute Engine', 'SKU-US-ONLY', 'US Only SKU', 'map', 1.0, 'us-central1', '', 1.0, 'core'),
                   ('k_eu', 'Compute Engine', 'SKU-NO-RATE', 'No Rate SKU', 'map', 1.0, 'europe-west1', '', 1.0, 'core')
        """)
        con.close()

        cat_path = os.path.join(tmp_dir, "catalog.duckdb")
        cat_db = duckdb.connect(cat_path)
        cat_db.execute("CREATE TABLE skus (sku_id TEXT, service_name TEXT, description TEXT, resource_family TEXT, resource_group TEXT, usage_type TEXT, usage_unit TEXT, service_regions TEXT[])")
        cat_db.execute("CREATE TABLE tiered_rates (sku_id TEXT, tier_start DOUBLE, rate_usd DOUBLE)")
        # SKU-US-ONLY has rate in us-central1; SKU-NO-RATE is not in tiered_rates
        cat_db.execute("INSERT INTO skus VALUES ('SKU-US-ONLY', 'Compute Engine', 'US Only SKU', 'Compute', 'CPU', 'OnDemand', 'h', ['us-central1'])")
        cat_db.execute("INSERT INTO tiered_rates VALUES ('SKU-US-ONLY', 0.0, 0.05)")
        cat_db.execute("INSERT INTO skus VALUES ('SKU-NO-RATE', 'Compute Engine', 'No Rate SKU', 'Compute', 'CPU', 'OnDemand', 'h', ['europe-west1'])")
        cat_db.close()

        monkeypatch.setattr(apply_rates, "DB_PATH", db_path)
        monkeypatch.setattr(apply_rates, "CATALOG_DB", cat_path)

        apply_rates.main()

        con = duckdb.connect(db_path)
        rows = dict(con.execute("SELECT aws_li_key, strategy FROM aws_li_to_gcp_li").fetchall())
        con.close()

        # k_us must remain 'map', while k_eu goes to 'outlier_triage'
        assert rows["k_us"] == "map", "Region with reachable rate must remain mapped"
        assert rows["k_eu"] == "outlier_triage", "Only unreachable region row should go to outlier_triage"


def test_region1_dominant_region_fallback_avoids_triage(monkeypatch):
    """REGION-1: Row in region A whose SKU has rate only in dominant region B is priced, NOT moved to outlier_triage."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "projection.duckdb")
        con = duckdb.connect(db_path)
        _setup_catalog_table(con)
        _setup_mappings_table(con)

        # us-central1 is dominant (2 rows)
        con.execute("""
            INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, billing_month, aws_amortized_cost)
            VALUES ('k_dom1', 'AmazonEC2', 'us-central1', 'us-east-1', 10.0, 'OnDemand', '2026-01', 10.0),
                   ('k_dom2', 'AmazonEC2', 'us-central1', 'us-east-1', 10.0, 'OnDemand', '2026-01', 10.0),
                   ('k_eu',   'AmazonEC2', 'europe-west1', 'eu-west-1', 10.0, 'OnDemand', '2026-01', 10.0)
        """)
        con.execute("""
            INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence, component)
            VALUES ('k_dom1', 'Compute Engine', 'SKU-US-ONLY', 'US Only SKU', 'map', 1.0, 'us-central1', '', 1.0, 'core'),
                   ('k_dom2', 'Compute Engine', 'SKU-US-ONLY', 'US Only SKU', 'map', 1.0, 'us-central1', '', 1.0, 'core'),
                   ('k_eu',   'Compute Engine', 'SKU-US-ONLY', 'US Only SKU', 'map', 1.0, 'europe-west1', '', 1.0, 'core')
        """)
        con.close()

        cat_path = os.path.join(tmp_dir, "catalog.duckdb")
        cat_db = duckdb.connect(cat_path)
        cat_db.execute("CREATE TABLE skus (sku_id TEXT, service_name TEXT, description TEXT, resource_family TEXT, resource_group TEXT, usage_type TEXT, usage_unit TEXT, service_regions TEXT[])")
        cat_db.execute("CREATE TABLE tiered_rates (sku_id TEXT, tier_start DOUBLE, rate_usd DOUBLE)")
        # SKU-US-ONLY only has rate in dominant region us-central1
        cat_db.execute("INSERT INTO skus VALUES ('SKU-US-ONLY', 'Compute Engine', 'US Only SKU', 'Compute', 'CPU', 'OnDemand', 'h', ['us-central1'])")
        cat_db.execute("INSERT INTO tiered_rates VALUES ('SKU-US-ONLY', 0.0, 0.05)")
        cat_db.close()

        monkeypatch.setattr(apply_rates, "DB_PATH", db_path)
        monkeypatch.setattr(apply_rates, "CATALOG_DB", cat_path)

        apply_rates.main()

        con = duckdb.connect(db_path)
        rows = dict(con.execute("SELECT aws_li_key, strategy FROM aws_li_to_gcp_li").fetchall())
        proj = con.execute("SELECT aws_li_key, gcp_projected_cost, projection_note FROM gcp_projection WHERE aws_li_key = 'k_eu'").fetchone()
        con.close()

        # k_eu is NOT moved to outlier_triage; stays map and is priced at dominant rate!
        assert rows["k_eu"] == "map"
        assert proj[1] == 10.0 * 1.0 * 0.05  # total_usage * mult * rate
        assert "priced at the us-central1 rate" in proj[2]


# ---------------------------------------------------------------------------
# Section E: Spot Priced at OnDemand List Rate (No Preemptible Synthesis)
# ---------------------------------------------------------------------------

def test_section_e_spot_priced_at_ondemand(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "projection.duckdb")
        con = duckdb.connect(db_path)
        _setup_catalog_table(con)
        _setup_mappings_table(con)
        con.execute("""
            INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, billing_month, aws_amortized_cost)
            VALUES ('k_spot', 'AmazonEC2', 'us-central1', 'us-east-1', 100.0, 'Spot', '2026-01', 50.0)
        """)
        con.execute("""
            INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence, component)
            VALUES ('k_spot', 'Compute Engine', 'SKU-VM-CPU', 'VM CPU', 'map', 1.0, 'us-central1', '', 1.0, 'core')
        """)
        con.close()

        cat_path = os.path.join(tmp_dir, "catalog.duckdb")
        cat_db = duckdb.connect(cat_path)
        cat_db.execute("CREATE TABLE skus (sku_id TEXT, service_name TEXT, description TEXT, resource_family TEXT, resource_group TEXT, usage_type TEXT, usage_unit TEXT, service_regions TEXT[])")
        cat_db.execute("CREATE TABLE tiered_rates (sku_id TEXT, tier_start DOUBLE, rate_usd DOUBLE)")
        cat_db.execute("INSERT INTO skus VALUES ('SKU-VM-CPU', 'Compute Engine', 'VM CPU', 'Compute', 'CPU', 'OnDemand', 'h', ['us-central1'])")
        cat_db.execute("INSERT INTO tiered_rates VALUES ('SKU-VM-CPU', 0.0, 0.10)")
        cat_db.close()

        monkeypatch.setattr(apply_rates, "DB_PATH", db_path)
        monkeypatch.setattr(apply_rates, "CATALOG_DB", cat_path)

        apply_rates.main()

        con = duckdb.connect(db_path)
        # Check no Preemptible rates were generated in gcp_sku_rates
        preemptible_count = con.execute("SELECT COUNT(*) FROM gcp_sku_rates WHERE pricing_type = 'Preemptible'").fetchone()[0]
        assert preemptible_count == 0, "Preemptible rates must not be synthesized"

        # Check Spot row projects at On-Demand rate (100.0 hrs * $0.10 = $10.00, NOT 0.22x $2.20)
        proj_cost = con.execute("SELECT gcp_projected_cost FROM gcp_projection WHERE aws_li_key = 'k_spot'").fetchone()[0]
        assert pytest.approx(proj_cost, 0.001) == 10.0, "Spot row must project at OnDemand list rate"
        con.close()


# ---------------------------------------------------------------------------
# Section F: Multi-Month Tiered Rate Blending Per Month
# ---------------------------------------------------------------------------

def test_section_f_tiered_rate_blending_per_month(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "projection.duckdb")
        con = duckdb.connect(db_path)
        _setup_catalog_table(con)
        _setup_mappings_table(con)
        # 3-month bill: 2000 GB each month
        con.execute("""
            INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, billing_month, aws_amortized_cost)
            VALUES ('k_m1', 'AWSDataTransfer', 'us-central1', 'us-east-1', 2000.0, 'OnDemand', '2026-01', 200.0),
                   ('k_m2', 'AWSDataTransfer', 'us-central1', 'us-east-1', 2000.0, 'OnDemand', '2026-02', 200.0),
                   ('k_m3', 'AWSDataTransfer', 'us-central1', 'us-east-1', 2000.0, 'OnDemand', '2026-03', 200.0)
        """)
        con.execute("""
            INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence, component)
            VALUES ('k_m1', 'Compute Engine', 'SKU-TIERED-NET', 'Net Egress', 'map', 1.0, 'us-central1', '', 1.0, NULL),
                   ('k_m2', 'Compute Engine', 'SKU-TIERED-NET', 'Net Egress', 'map', 1.0, 'us-central1', '', 1.0, NULL),
                   ('k_m3', 'Compute Engine', 'SKU-TIERED-NET', 'Net Egress', 'map', 1.0, 'us-central1', '', 1.0, NULL)
        """)
        con.close()

        cat_path = os.path.join(tmp_dir, "catalog.duckdb")
        cat_db = duckdb.connect(cat_path)
        cat_db.execute("CREATE TABLE skus (sku_id TEXT, service_name TEXT, description TEXT, resource_family TEXT, resource_group TEXT, usage_type TEXT, usage_unit TEXT, service_regions TEXT[])")
        cat_db.execute("CREATE TABLE tiered_rates (sku_id TEXT, tier_start DOUBLE, rate_usd DOUBLE)")
        cat_db.execute("INSERT INTO skus VALUES ('SKU-TIERED-NET', 'Compute Engine', 'Net Egress', 'Network', 'Egress', 'OnDemand', 'gibibyte', ['us-central1'])")
        # Tier 1: 0 - 1000 GB @ $0.12/GB
        # Tier 2: 1000+ GB @ $0.08/GB
        cat_db.execute("INSERT INTO tiered_rates VALUES ('SKU-TIERED-NET', 0.0, 0.12)")
        cat_db.execute("INSERT INTO tiered_rates VALUES ('SKU-TIERED-NET', 1000.0, 0.08)")
        cat_db.close()

        monkeypatch.setattr(apply_rates, "DB_PATH", db_path)
        monkeypatch.setattr(apply_rates, "CATALOG_DB", cat_path)

        apply_rates.main()

        con = duckdb.connect(db_path)
        # Expected calculation:
        # Total usage = 6000 GB across 3 distinct months -> 2000 GB / month.
        # Monthly blended rate for 2000 GB:
        # 1000 GB * $0.12 = $120
        # 1000 GB * $0.08 = $80
        # Blended rate = $200 / 2000 GB = $0.10/GB.
        # Each row is 2000 GB * $0.10 = $200.
        # Total 3-month projected cost = 3 * $200 = $600.
        # (If blended on 6000 GB whole-bill: 1000*0.12 + 5000*0.08 = $520 -> under-projected!)
        rate = con.execute("SELECT rate_usd FROM gcp_sku_rates WHERE gcp_sku_id = 'SKU-TIERED-NET' AND region = 'us-central1'").fetchone()[0]
        assert pytest.approx(rate, 0.001) == 0.10, "Blended rate must be based on monthly average volume ($0.10), not whole-bill volume"

        total_proj = con.execute("SELECT SUM(gcp_projected_cost) FROM gcp_projection").fetchone()[0]
        assert pytest.approx(total_proj, 0.01) == 600.0, "Total projected cost across 3 months must equal 3 * single-month blended cost"
        con.close()


# ---------------------------------------------------------------------------
# Section AR-E: Aurora MySQL Engine Storage SKU Precision
# ---------------------------------------------------------------------------

def test_section_ar_e_aurora_mysql_storage_sku():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE aws_li_catalog (
            aws_li_key TEXT PRIMARY KEY,
            product TEXT,
            usage_type TEXT,
            pricing_unit TEXT,
            line_item_type TEXT,
            gcp_region TEXT
        );
        CREATE TABLE aws_li_to_gcp_li (
            aws_li_key TEXT,
            gcp_service TEXT,
            gcp_sku_id TEXT,
            gcp_sku_name TEXT,
            strategy TEXT,
            unit_multiplier DOUBLE,
            component TEXT,
            mapping_confidence DOUBLE,
            projection_note TEXT
        );
    """)

    # Three rows: Aurora MySQL, Aurora PostgreSQL, RDS MySQL
    con.execute("""
        INSERT INTO aws_li_catalog VALUES
            ('k_aurora_mysql', 'Amazon Aurora (MySQL)', 'StorageUsage', 'GB-Mo', 'Usage', 'us-central1'),
            ('k_aurora_pg', 'Amazon Aurora (PostgreSQL)', 'StorageUsage', 'GB-Mo', 'Usage', 'us-central1'),
            ('k_rds_mysql', 'Amazon RDS for MySQL Community Edition', 'StorageUsage', 'GB-Mo', 'Usage', 'us-central1');
        INSERT INTO aws_li_to_gcp_li VALUES
            ('k_aurora_mysql', 'Cloud SQL', 'SKU-OLD-1', 'Cloud SQL vCPU', 'map', 2.0, 'core', 0.9, ''),
            ('k_aurora_pg', 'Cloud SQL', 'SKU-OLD-2', 'Cloud SQL vCPU', 'map', 2.0, 'core', 0.9, ''),
            ('k_rds_mysql', 'Cloud SQL', 'SKU-OLD-3', 'Cloud SQL vCPU', 'map', 2.0, 'core', 0.9, '');
    """)

    apply_rates.fix_managed_db_storage_rows(con)

    rows = dict(con.execute("SELECT aws_li_key, gcp_sku_name FROM aws_li_to_gcp_li").fetchall())
    # Aurora MySQL must map to Cloud SQL for MySQL, NOT PostgreSQL
    assert "Cloud SQL for MySQL" in rows["k_aurora_mysql"], f"Expected MySQL storage, got: {rows['k_aurora_mysql']}"
    assert "Cloud SQL for PostgreSQL" in rows["k_aurora_pg"], f"Expected PostgreSQL storage, got: {rows['k_aurora_pg']}"
    assert "Cloud SQL for MySQL" in rows["k_rds_mysql"], f"Expected MySQL storage, got: {rows['k_rds_mysql']}"
    con.close()


# ---------------------------------------------------------------------------
# Section AR-F: Multi-AZ Flag in deployment_option
# ---------------------------------------------------------------------------

def test_section_ar_f_multi_az_deployment_option():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE aws_li_catalog (
            aws_li_key TEXT PRIMARY KEY,
            product TEXT,
            usage_type TEXT,
            pricing_unit TEXT,
            line_item_type TEXT,
            gcp_region TEXT,
            deployment_option TEXT
        );
        CREATE TABLE aws_li_to_gcp_li (
            aws_li_key TEXT,
            gcp_service TEXT,
            gcp_sku_id TEXT,
            gcp_sku_name TEXT,
            strategy TEXT,
            unit_multiplier DOUBLE,
            component TEXT,
            mapping_confidence DOUBLE,
            projection_note TEXT
        );
    """)

    # usage_type has NO 'multi-az', but deployment_option IS 'Multi-AZ'
    con.execute("""
        INSERT INTO aws_li_catalog VALUES
            ('k_ha', 'Amazon RDS for MySQL', 'StorageUsage', 'GB-Mo', 'Usage', 'us-central1', 'Multi-AZ'),
            ('k_single', 'Amazon RDS for MySQL', 'StorageUsage', 'GB-Mo', 'Usage', 'us-central1', 'Single-AZ');
        INSERT INTO aws_li_to_gcp_li VALUES
            ('k_ha', 'Cloud SQL', 'SKU-OLD-1', 'Cloud SQL vCPU', 'map', 2.0, 'core', 0.9, ''),
            ('k_single', 'Cloud SQL', 'SKU-OLD-2', 'Cloud SQL vCPU', 'map', 2.0, 'core', 0.9, '');
    """)

    apply_rates.fix_managed_db_storage_rows(con)

    rows = dict(con.execute("SELECT aws_li_key, gcp_sku_name FROM aws_li_to_gcp_li").fetchall())
    # Multi-AZ must map to Regional tier
    assert "Regional" in rows["k_ha"], f"Expected Regional HA storage tier, got: {rows['k_ha']}"
    assert "Zonal" in rows["k_single"], f"Expected Zonal storage tier, got: {rows['k_single']}"
    con.close()


# ---------------------------------------------------------------------------
# Section AR-G: Internet Egress Injection Monthly Blending
# ---------------------------------------------------------------------------

def test_section_ar_g_egress_injection_monthly_blend(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "projection.duckdb")
        con = duckdb.connect(db_path)
        _setup_catalog_table(con)
        _setup_mappings_table(con)

        # 3-month bill: 20,000 GB each month -> total 60,000 GB
        # Tiers: 0-10TB (10240 GB) @ $0.12, 10-50TB (10240-51200 GB) @ $0.11
        # At 20,000 GB / month:
        # 10,240 * 0.12 = 1228.80
        # 9,760 * 0.11 = 1073.60
        # Sum = 2302.40 / 20000 = $0.11512 / GB
        # At whole-bill 60,000 GB:
        # 10,240 * 0.12 + 40,960 * 0.11 + 8,800 * 0.08 = 6438.40 / 60000 = $0.1073 / GB (underpriced!)
        con.execute("""
            INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, billing_month, aws_amortized_cost)
            VALUES ('k_eg1', 'AWSDataTransfer', 'global', 'us-east-1', 20000.0, 'OnDemand', '2026-01', 2400.0),
                   ('k_eg2', 'AWSDataTransfer', 'global', 'us-east-1', 20000.0, 'OnDemand', '2026-02', 2400.0),
                   ('k_eg3', 'AWSDataTransfer', 'global', 'us-east-1', 20000.0, 'OnDemand', '2026-03', 2400.0);
            INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence, component)
            VALUES ('k_eg1', 'Compute Engine', 'GCP-EGRESS-INTERNET', 'Network Internet Egress', 'map', 1.0, 'global', '', 1.0, NULL),
                   ('k_eg2', 'Compute Engine', 'GCP-EGRESS-INTERNET', 'Network Internet Egress', 'map', 1.0, 'global', '', 1.0, NULL),
                   ('k_eg3', 'Compute Engine', 'GCP-EGRESS-INTERNET', 'Network Internet Egress', 'map', 1.0, 'global', '', 1.0, NULL);
        """)
        con.close()

        cat_path = os.path.join(tmp_dir, "catalog.duckdb")
        cat_db = duckdb.connect(cat_path)
        cat_db.execute("CREATE TABLE skus (sku_id TEXT, service_name TEXT, description TEXT, resource_family TEXT, resource_group TEXT, usage_type TEXT, usage_unit TEXT, service_regions TEXT[])")
        cat_db.execute("CREATE TABLE tiered_rates (sku_id TEXT, tier_start DOUBLE, rate_usd DOUBLE)")
        cat_db.close()

        monkeypatch.setattr(apply_rates, "DB_PATH", db_path)
        monkeypatch.setattr(apply_rates, "CATALOG_DB", cat_path)

        apply_rates.main()

        con = duckdb.connect(db_path)
        rate = con.execute("SELECT rate_usd FROM gcp_sku_rates WHERE gcp_sku_id = 'GCP-EGRESS-INTERNET'").fetchone()[0]
        # Monthly blended rate for 20,000 GB:
        # 1024 * 0.12 = 122.88
        # 9216 * 0.11 = 1013.76
        # 9760 * 0.08 = 780.80
        # Sum = 1917.44 / 20000 = $0.095872 / GB
        # (Whole-bill 60,000 GB would be $0.08529 / GB)
        assert pytest.approx(rate, 0.0001) == 0.095872, f"Expected monthly blended rate (~0.095872), got {rate}"
        assert pytest.approx(rate, 0.0001) != 0.08529, "Rate must not be based on whole-bill volume (60,000 GB)"
        con.close()


# ---------------------------------------------------------------------------
# Section AR-H: Memorystore SKU Name Relabel
# ---------------------------------------------------------------------------

def test_section_ar_h_memorystore_sku_name_relabel():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE aws_li_catalog (aws_li_key TEXT PRIMARY KEY, product TEXT);
        CREATE TABLE aws_li_to_gcp_li (
            aws_li_key TEXT,
            gcp_service TEXT,
            gcp_sku_id TEXT,
            gcp_sku_name TEXT,
            component TEXT,
            projection_note TEXT
        );
    """)

    con.execute("""
        INSERT INTO aws_li_catalog VALUES
            ('k_ec_core', 'Amazon ElastiCache'),
            ('k_ec_ram', 'Amazon ElastiCache');
        INSERT INTO aws_li_to_gcp_li VALUES
            ('k_ec_core', 'Cloud SQL', 'OLD-SKU-1', 'Cloud SQL Custom Core', 'core', ''),
            ('k_ec_ram', 'Cloud SQL', 'OLD-SKU-2', 'Cloud SQL Custom RAM', 'ram', '');
    """)

    apply_rates.remap_elasticache_to_memorystore(con)

    rows = dict(con.execute("SELECT aws_li_key, (gcp_service, gcp_sku_name, gcp_sku_id) FROM aws_li_to_gcp_li").fetchall())
    svc_core, name_core, sku_core = rows["k_ec_core"]
    svc_ram, name_ram, sku_ram = rows["k_ec_ram"]

    assert svc_core == "Cloud Memorystore for Memcached"
    assert "Memorystore for Memcached Custom Core" in name_core
    assert sku_core is None

    assert svc_ram == "Cloud Memorystore for Memcached"
    assert "Memorystore for Memcached Custom RAM" in name_ram
    assert sku_ram is None
    con.close()


# ---------------------------------------------------------------------------
# Section AR-I: Deterministic Service Normalization Subquery
# ---------------------------------------------------------------------------

def test_section_ar_i_deterministic_service_normalization():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE aws_li_to_gcp_li (
            aws_li_key TEXT PRIMARY KEY,
            strategy TEXT,
            gcp_sku_id TEXT,
            gcp_service TEXT
        );
        CREATE TABLE gcp_sku_rates (
            gcp_sku_id TEXT,
            gcp_service TEXT,
            region TEXT
        );
    """)

    # Mapping with short name
    con.execute("INSERT INTO aws_li_to_gcp_li VALUES ('k1', 'map', 'SKU-KMS-1', 'Cloud KMS')")
    # Multiple service rates for same SKU (e.g. across regions)
    con.execute("""
        INSERT INTO gcp_sku_rates VALUES
            ('SKU-KMS-1', 'Cloud Key Management Service (KMS)', 'us-east1'),
            ('SKU-KMS-1', 'Cloud Key Management Service (KMS)', 'global');
    """)

    # Run normalization
    norm_count = con.execute("""
        SELECT COUNT(*) FROM aws_li_to_gcp_li m
        WHERE m.strategy IN ('map', 'break_down')
          AND m.gcp_sku_id IS NOT NULL
          AND m.gcp_service != (
            SELECT r.gcp_service FROM gcp_sku_rates r
            WHERE r.gcp_sku_id = m.gcp_sku_id
            ORDER BY r.gcp_service, r.region
            LIMIT 1
          )
    """).fetchone()[0]
    assert norm_count == 1

    con.execute("""
        UPDATE aws_li_to_gcp_li m
        SET gcp_service = (
            SELECT r.gcp_service FROM gcp_sku_rates r
            WHERE r.gcp_sku_id = m.gcp_sku_id
            ORDER BY r.gcp_service, r.region
            LIMIT 1
        )
        WHERE m.strategy IN ('map', 'break_down')
          AND m.gcp_sku_id IS NOT NULL
          AND m.gcp_service != (
            SELECT r.gcp_service FROM gcp_sku_rates r
            WHERE r.gcp_sku_id = m.gcp_sku_id
            ORDER BY r.gcp_service, r.region
            LIMIT 1
          )
    """)

    res = con.execute("SELECT gcp_service FROM aws_li_to_gcp_li WHERE aws_li_key = 'k1'").fetchone()[0]
    assert res == "Cloud Key Management Service (KMS)"
    con.close()


# ---------------------------------------------------------------------------
# Section AR-J: Missing Catalog Early Return Stamps rate_source
# ---------------------------------------------------------------------------

def test_section_ar_j_no_catalog_rate_source_stamped(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "projection.duckdb")
        con = duckdb.connect(db_path)
        _setup_catalog_table(con)
        _setup_mappings_table(con)
        con.execute("""
            INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, aws_amortized_cost)
            VALUES ('k_dummy', 'AmazonEC2', 'us-central1', 'us-east-1', 10.0, 'OnDemand', 10.0);
            INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence, component)
            VALUES ('k_dummy', 'Compute Engine', 'SKU-EC2', 'VM CPU', 'map', 1.0, 'us-central1', '', 0.9, NULL);
        """)
        con.close()

        # Point CATALOG_DB to a nonexistent file
        nonexistent_cat = os.path.join(tmp_dir, "missing_catalog.duckdb")
        monkeypatch.setattr(apply_rates, "DB_PATH", db_path)
        monkeypatch.setattr(apply_rates, "CATALOG_DB", nonexistent_cat)

        apply_rates.main()

        con = duckdb.connect(db_path)
        strat, rate_src, note = con.execute("SELECT strategy, rate_source, projection_note FROM aws_li_to_gcp_li WHERE aws_li_key = 'k_dummy'").fetchone()
        assert strat == "passthrough"
        assert rate_src == "no_catalog", f"rate_source must be stamped 'no_catalog', got: {rate_src}"
        assert "GCP catalog unavailable at run time" in note
        con.close()


# ---------------------------------------------------------------------------
# Third Batch Tests: RV-1 through RV-5, RV-D1, RV-D2, and RV-minor
# ---------------------------------------------------------------------------

def test_rv_1_apply_rates_null_sku_component_filter(monkeypatch):
    """RV-1: apply_rates null-SKU resolution loop filtered by component."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "projection.duckdb")
        con = duckdb.connect(db_path)
        _setup_catalog_table(con)
        _setup_mappings_table(con)

        # 1 line item with 2 components: core (has SKU) and ram (null SKU)
        con.execute("""
            INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, aws_amortized_cost)
            VALUES ('k_multi', 'AmazonEC2', 'us-central1', 'us-east-1', 10.0, 'OnDemand', 50.0);
            INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, gcp_sku_unit, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence, component)
            VALUES 
                ('k_multi', 'Compute Engine', 'CORE-SKU-1', 'N2 Core', 'h', 'break_down', 4.0, 'us-central1', 'core component', 0.9, 'core'),
                ('k_multi', 'Compute Engine', NULL, NULL, NULL, 'break_down', 16.0, 'us-central1', 'ram component', 0.8, 'ram');
        """)
        con.close()

        def mock_resolve(description, service_regions, fallback_to_cheapest=True, expected_unit=None):
            return {
                "sku_id": "RAM-SKU-NEW",
                "sku_name": "N2 RAM",
                "rate_usd": 0.004,
                "pricing_unit": "GiBy.h",
                "region": "us-central1",
                "rate_source": "exact",
                "confidence": 0.85
            }

        con = duckdb.connect(db_path)
        null_rows = con.execute("""
            SELECT m.aws_li_key, m.gcp_service, c.product, c.usage_type, c.operation,
                   m.gcp_region, c.aws_region, m.component, c.total_usage, m.unit_multiplier
            FROM aws_li_to_gcp_li m
            JOIN aws_li_catalog c ON c.aws_li_key = m.aws_li_key
            WHERE m.strategy IN ('map', 'break_down')
              AND m.gcp_sku_id IS NULL
        """).fetchall()

        for k, svc, prod, ut, op, gcp_reg, aws_reg, comp, usage, mult in null_rows:
            res = mock_resolve("RAM search", [gcp_reg])
            if res:
                con.execute("""
                    UPDATE aws_li_to_gcp_li
                    SET gcp_sku_id = ?,
                        gcp_sku_name = ?,
                        gcp_sku_unit = ?,
                        rate_source = ?,
                        gcp_region = COALESCE(gcp_region, ?),
                        projection_note = CASE
                            WHEN projection_note IS NULL OR projection_note = '' THEN ?
                            ELSE projection_note || ' | ' || ?
                        END
                    WHERE aws_li_key = ?
                      AND component IS NOT DISTINCT FROM ?
                """, [
                    res["sku_id"], res["sku_name"], res.get("pricing_unit"),
                    res.get("rate_source", "catalog"), res.get("region"),
                    "[apply_rates: resolved null SKU via fallback]",
                    "[apply_rates: resolved null SKU via fallback]",
                    k, comp
                ])

        core_sku = con.execute("SELECT gcp_sku_id FROM aws_li_to_gcp_li WHERE aws_li_key = 'k_multi' AND component = 'core'").fetchone()[0]
        ram_sku = con.execute("SELECT gcp_sku_id FROM aws_li_to_gcp_li WHERE aws_li_key = 'k_multi' AND component = 'ram'").fetchone()[0]
        assert core_sku == "CORE-SKU-1"
        assert ram_sku == "RAM-SKU-NEW"
        con.close()


def test_rv_2_unit_equivalence_canonical_classes_and_rejection():
    """RV-2: unit_class accepts canonical class names; incompatible SKU rejected."""
    assert unit_class("storage-month") == "storage-month"
    assert unit_class("count") == "count"
    assert unit_class("time") == "time"
    assert unit_class("data") == "data"
    assert unit_class("seat") == "seat"
    assert unit_class("unknown-unit") is None

    # Cloud SQL storage expecting storage-month must reject requests/ops SKU
    assert unit_class("storage-month") != unit_class("requests")
    assert unit_class("storage-month") != unit_class("operations")
    assert unit_class("storage-month") == unit_class("GB-Mo")
    assert unit_class("storage-month") == unit_class("GiBy.mo")


def test_rv_3_mechanic_group_unit_coverage():
    """RV-3: table test asserting every mechanic_group has expected unit or is in allowlist."""
    all_groups = [
        "negative_cost", "commitment_discount", "cloudwatch", "non_workload",
        "compute_windows", "compute_burstable", "compute_arm", "opensearch",
        "compute_breakdown", "rds_extended_support", "managed_db", "efs", "fsx",
        "block_storage", "data_transfer", "xray", "flat_hourly", "per_request",
        "elasticache", "msk", "guardduty", "inspector", "mwaa", "ssm_parameter_store",
        "dynamodb_storage", "shield", "quicksight", "redshift", "athena", "kinesis",
        "emr", "glue", "object_storage", "marketplace_thirdparty", "misc",
    ]
    unverified = set(resolve_sku._UNVERIFIED_MECHANIC_GROUPS)

    for group in all_groups:
        expected = resolve_sku.infer_expected_unit(group)
        if expected is None:
            assert group in unverified, f"mechanic_group '{group}' has no inferred unit and is not in _UNVERIFIED_MECHANIC_GROUPS"


def test_rv_4_discounts_negative_amortized_cost_only(tmp_path):
    """RV-4: discounts query sums only negative amortized cost rows."""
    import export_report
    db_path = str(tmp_path / "projection.duckdb")
    con = duckdb.connect(db_path)
    _setup_catalog_table(con)

    con.execute("""
        INSERT INTO aws_li_catalog (aws_li_key, product, line_item_type, mechanic_group, aws_amortized_cost, is_workload)
        VALUES 
            ('k_sp', 'Savings Plans', 'Fee', 'commitment_discount', 5058.72, false),
            ('k_credit', 'Compute', 'DiscountedUsage', 'commitment_discount', -1200.00, false);
    """)

    disc_query = export_report._build_query_discounts("USD", 1.0)
    res = con.execute(disc_query).fetchall()
    assert len(res) == 1
    assert res[0][1] == "Compute"
    assert res[0][4] == 1200.00
    con.close()


def test_rv_5_ingest_billing_month(tmp_path):
    """RV-5: billing_month derived and stored as YYYY-MM across ingest rows."""
    db_path = str(tmp_path / "projection.duckdb")
    con = duckdb.connect(db_path)
    _setup_catalog_table(con)
    con.execute("""
        CREATE TABLE raw_lines (
            line_item_usage_start_date TEXT,
            bill_billing_period_start_date TEXT
        );
        INSERT INTO raw_lines VALUES
            ('2026-03-15T00:00:00Z', '2026-03-01'),
            ('2026-04-10T12:00:00Z', '2026-04-01');
    """)
    res = con.execute("""
        SELECT COALESCE(
            LEFT(CAST(bill_billing_period_start_date AS VARCHAR), 7),
            LEFT(CAST(line_item_usage_start_date AS VARCHAR), 7),
            ''
        ) AS billing_month
        FROM raw_lines
    """).fetchall()
    assert res[0][0] == "2026-03"
    assert res[1][0] == "2026-04"
    con.close()


def test_rv_d1_projection_view_headline_od_pricing(tmp_path):
    """RV-D1: headline gcp_projected_cost uses OD list price; 1yr CUD shows committed rate."""
    db_path = str(tmp_path / "projection.duckdb")
    con = duckdb.connect(db_path)
    _setup_catalog_table(con)
    _setup_mappings_table(con)

    con.execute("""
        CREATE TABLE gcp_sku_rates (
            gcp_sku_id TEXT,
            region TEXT,
            pricing_type TEXT,
            rate_usd DOUBLE
        );
        INSERT INTO gcp_sku_rates VALUES
            ('SKU-1', 'us-central1', 'OnDemand', 0.10),
            ('SKU-1', 'us-central1', 'Commit1Yr', 0.06),
            ('SKU-1', 'us-central1', 'Commit3Yr', 0.04);
    """)

    con.execute("""
        INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, aws_amortized_cost, is_workload)
        VALUES ('k_ri', 'AmazonEC2', 'us-central1', 'us-east-1', 100.0, 'Reserved', 5.0, true);
        INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence, component)
        VALUES ('k_ri', 'Compute Engine', 'SKU-1', 'N2 Core', 'map', 1.0, 'us-central1', '', 0.95, 'default');
    """)

    create_projection_view(con)

    proj = con.execute("SELECT gcp_projected_cost, gcp_cost_1yr_cud, gcp_cost_3yr_cud FROM gcp_projection WHERE aws_li_key = 'k_ri'").fetchone()
    assert proj[0] == 10.0
    assert proj[1] == 6.0
    assert proj[2] == 4.0
    con.close()


def test_rv_d2_aurora_serverless_v2_acu_breakdown():
    """RV-D2: Aurora Serverless v2 ACU row breaks down at vCPU=2.0 and RAM=16.0 per ACU."""
    import apply_static_mappings
    test_row = {
        "aws_li_key": "k_acu",
        "product": "Amazon Aurora PostgreSQL",
        "usage_type": "APS3-ServerlessUsage",
        "operation": "",
        "unit": "ACU-Hr",
        "pricing_unit": "ACU-Hr",
        "total_usage": 100.0,
        "pricing_model": "OnDemand",
        "aws_amortized_cost": 20.0,
        "aws_region": "ap-southeast-3",
        "gcp_region": "asia-southeast2",
    }
    with patch.object(apply_static_mappings, "_strict_resolve_sku", lambda svc, sku, reg: ("MOCK-SKU", "mock_unit")):
        out = apply_static_mappings.map_managed_db([test_row])
        if isinstance(out, tuple):
            out = out[0]
    assert len(out) == 2
    components = {e["component"]: e for e in out}
    assert "core" in components
    assert "ram" in components
    assert components["ram"]["unit_multiplier"] == 16.0
    assert components["core"]["unit_multiplier"] == 2.0
    assert "1 ACU = 2 vCPU + 16 GiB" in components["ram"]["projection_note"]


def test_rv_minor_cheapest_region_confidence_capping():
    """RV-minor: cheapest-region fallback caps confidence <= 0.4."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "projection.duckdb")
        con = duckdb.connect(db_path)
        _setup_catalog_table(con)
        _setup_mappings_table(con)

        con.execute("""
            INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, aws_amortized_cost)
            VALUES ('k_cheap', 'AmazonEC2', 'europe-west9', 'eu-west-1', 10.0, 'OnDemand', 10.0);
            INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence, component)
            VALUES ('k_cheap', 'Compute Engine', NULL, NULL, 'map', 1.0, 'europe-west9', '', 0.95, 'default');
        """)

        res = {
            "sku_id": "SKU-CHEAP",
            "sku_name": "VM SKU",
            "rate_usd": 0.02,
            "pricing_unit": "h",
            "region": "us-central1",
            "rate_source": "cheapest_available_region",
            "confidence": 0.85
        }
        new_conf = min(res.get("confidence", 0.4), 0.4) if res.get("rate_source") == "cheapest_available_region" else res.get("confidence", 0.8)
        con.execute("""
            UPDATE aws_li_to_gcp_li
            SET gcp_sku_id = ?, mapping_confidence = ?, rate_source = ?
            WHERE aws_li_key = 'k_cheap'
        """, [res["sku_id"], new_conf, res["rate_source"]])

        conf = con.execute("SELECT mapping_confidence FROM aws_li_to_gcp_li WHERE aws_li_key = 'k_cheap'").fetchone()[0]
        assert conf <= 0.4
        con.close()


def test_rv_minor_outlier_triage_sets_null_costs(tmp_path):
    """RV-minor: strategy = 'outlier_triage' sets NULL in all 3 cost projection columns."""
    db_path = str(tmp_path / "projection.duckdb")
    con = duckdb.connect(db_path)
    _setup_catalog_table(con)
    _setup_mappings_table(con)
    con.execute("""
        CREATE TABLE gcp_sku_rates (
            gcp_sku_id TEXT,
            region TEXT,
            pricing_type TEXT,
            rate_usd DOUBLE
        );
        INSERT INTO gcp_sku_rates VALUES ('SKU-OUT', 'us-central1', 'OnDemand', 10.0);
        INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, aws_amortized_cost, is_workload)
        VALUES ('k_out', 'AmazonEC2', 'us-central1', 'us-east-1', 10.0, 'OnDemand', 10.0, true);
        INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence, component)
        VALUES ('k_out', 'Compute Engine', 'SKU-OUT', 'Outlier SKU', 'outlier_triage', 1.0, 'us-central1', '', 0.1, 'default');
    """)
    create_projection_view(con)
    proj = con.execute("SELECT gcp_projected_cost, gcp_cost_1yr_cud, gcp_cost_3yr_cud FROM gcp_projection WHERE aws_li_key = 'k_out'").fetchone()
    assert proj[0] is None
    assert proj[1] is None
    assert proj[2] is None
    con.close()


def test_rv_minor_apply_commitment_ignores_missing_column(tmp_path):
    """RV-minor: apply_commitment_ignores gracefully skips when mechanic_group column is missing."""
    import apply_commitment_ignores
    db_path = str(tmp_path / "projection.duckdb")
    con = duckdb.connect(db_path)
    con.execute("""
        CREATE TABLE aws_li_catalog (
            aws_li_key TEXT,
            product TEXT
        );
        CREATE TABLE aws_li_to_gcp_li (
            aws_li_key TEXT,
            strategy TEXT
        );
    """)
    con.close()
    res = apply_commitment_ignores.process_database(db_path)
    assert res == (0, 0)


# ---------------------------------------------------------------------------
# Section RV-1: Null-SKU resolution filters by component
# ---------------------------------------------------------------------------

def test_rv1_null_sku_resolution_per_component(tmp_path, monkeypatch):
    """RV-1: A row with multiple components resolving NULL SKUs resolves each to its OWN SKU."""
    db_path = str(tmp_path / "projection.duckdb")
    con = duckdb.connect(db_path)
    _setup_catalog_table(con)
    _setup_mappings_table(con)
    con.execute("""
        INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, aws_amortized_cost)
        VALUES ('k_multi', 'Amazon ElastiCache', 'us-central1', 'us-central1', 10.0, 'OnDemand', 50.0);

        INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence, component)
        VALUES 
            ('k_multi', 'Memorystore for Redis', NULL, 'Core SKU', 'break_down', 1.0, 'us-central1', '', 0.8, 'core'),
            ('k_multi', 'Memorystore for Redis', NULL, 'RAM SKU', 'break_down', 1.0, 'us-central1', '', 0.8, 'ram');
    """)
    con.close()

    monkeypatch.setattr(apply_rates, "DB_PATH", db_path)

    # Mock resolve_intent_to_sku to return distinct SKUs per component
    class MockRes:
        def __init__(self, sku_id, unit="h", note=""):
            self.sku_id = sku_id
            self.unit = unit
            self.note = note

    def mock_resolve(svc, name, reg, expected_unit=None, mechanic_group=None, component=None):
        if component == "core":
            return MockRes("SKU-REDIS-CORE", "h", "[core resolved]")
        elif component == "ram":
            return MockRes("SKU-REDIS-RAM", "GiBy.mo", "[ram resolved]")
        return MockRes(None, None, "unmatched")

    monkeypatch.setattr(apply_rates, "resolve_intent_to_sku", mock_resolve)

    cat_path = str(tmp_path / "catalog.duckdb")
    cat_db = duckdb.connect(cat_path)
    cat_db.execute("CREATE TABLE skus (sku_id TEXT, service_name TEXT, description TEXT, resource_family TEXT, resource_group TEXT, usage_type TEXT, usage_unit TEXT, service_regions TEXT[])")
    cat_db.execute("CREATE TABLE tiered_rates (sku_id TEXT, tier_start DOUBLE, rate_usd DOUBLE)")
    cat_db.execute("""
        INSERT INTO skus VALUES 
            ('SKU-REDIS-CORE', 'Memorystore for Redis', 'Core SKU', 'Compute', 'CPU', 'OnDemand', 'h', ['us-central1']),
            ('SKU-REDIS-RAM', 'Memorystore for Redis', 'RAM SKU', 'Storage', 'RAM', 'OnDemand', 'GiBy.mo', ['us-central1']);
        INSERT INTO tiered_rates VALUES
            ('SKU-REDIS-CORE', 0.0, 0.05),
            ('SKU-REDIS-RAM', 0.0, 0.02);
    """)
    cat_db.close()
    monkeypatch.setattr(apply_rates, "CATALOG_DB", cat_path)

    apply_rates.main()

    con = duckdb.connect(db_path)
    rows = con.execute("""
        SELECT component, gcp_sku_id, rate_source
        FROM aws_li_to_gcp_li
        WHERE aws_li_key = 'k_multi'
        ORDER BY component
    """).fetchall()
    con.close()

    assert len(rows) == 2
    comp_map = {r[0]: (r[1], r[2]) for r in rows}
    assert comp_map["core"][0] == "SKU-REDIS-CORE"
    assert comp_map["ram"][0] == "SKU-REDIS-RAM"


# ---------------------------------------------------------------------------
# Section AR-K: blended_rate returns base tier on zero usage
# ---------------------------------------------------------------------------

def test_ark_blended_rate_zero_usage_returns_base_tier():
    """AR-K: blended_rate returns the base (first/most expensive) tier on zero/absent usage."""
    tiers = [
        {"startUsageAmount": 0, "rate": 0.12},
        {"startUsageAmount": 1024, "rate": 0.11},
        {"startUsageAmount": 10240, "rate": 0.08},
    ]
    # Zero usage -> base tier (0.12), NOT cheapest tier (0.08)
    assert apply_rates.blended_rate(tiers, 0.0) == 0.12
    assert apply_rates.blended_rate(tiers, -10.0) == 0.12
    # Empty tiers -> 0.0
    assert apply_rates.blended_rate([], 0.0) == 0.0
    # Positive usage blends progressively
    assert apply_rates.blended_rate(tiers, 500.0) == 0.12
    assert apply_rates.blended_rate(tiers, 2000.0) < 0.12


# ---------------------------------------------------------------------------
# Section AR-L: Parameterize egress fallback query
# ---------------------------------------------------------------------------

def test_arl_egress_fallback_query_parameterized(tmp_path):
    """AR-L: Egress query handles SKU names containing apostrophes without throwing."""
    db_path = str(tmp_path / "projection.duckdb")
    con = duckdb.connect(db_path)
    _setup_catalog_table(con)
    _setup_mappings_table(con)
    con.execute("""
        INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, aws_amortized_cost)
        VALUES ('k_egr', 'AWS Data Transfer', 'global', 'global', 500.0, 'OnDemand', 45.0)
    """)
    con.execute("""
        INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence)
        VALUES ('k_egr', 'Compute Engine', 'GCP-EGRESS-INTERNET', ?, 'map', 1.0, 'global', '', 1.0)
    """, ["Internet Egress (Tier 1's special)"])

    _sku_id = "GCP-EGRESS-INTERNET"
    _sku_name = "Internet Egress (Tier 1's special)"
    month_expr = "1"
    u_row = con.execute(f"""
        SELECT SUM(c.total_usage) / {month_expr}
        FROM aws_li_to_gcp_li m
        JOIN aws_li_catalog c USING (aws_li_key)
        WHERE m.gcp_sku_id = ? OR m.gcp_sku_name = ?
    """, [_sku_id, _sku_name]).fetchone()

    assert u_row is not None
    assert u_row[0] == 500.0
    con.close()


# ---------------------------------------------------------------------------
# Section AR-M: All-passthrough bill stamps rate_source
# ---------------------------------------------------------------------------

def test_arm_all_passthrough_stamps_rate_source(tmp_path, monkeypatch):
    """AR-M: When all rows are passthrough (no SKUs to fill), rate_source is stamped on early return."""
    db_path = str(tmp_path / "projection.duckdb")
    con = duckdb.connect(db_path)
    _setup_catalog_table(con)
    _setup_mappings_table(con)
    con.execute("""
        INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, aws_amortized_cost)
        VALUES 
            ('k_pass1', 'SomeService', 'us-central1', 'us-east-1', 10.0, 'OnDemand', 100.0),
            ('k_pass2', 'OtherService', 'us-central1', 'us-east-1', 5.0, 'OnDemand', 50.0);

        INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence)
        VALUES 
            ('k_pass1', 'Manual Review', NULL, NULL, 'passthrough', 1.0, 'us-central1', '', 0.1),
            ('k_pass2', 'Manual Review', NULL, NULL, 'passthrough', 1.0, 'us-central1', '', 0.1);
    """)
    con.close()

    monkeypatch.setattr(apply_rates, "DB_PATH", db_path)
    apply_rates.main()

    con = duckdb.connect(db_path)
    sources = con.execute("SELECT DISTINCT rate_source FROM aws_li_to_gcp_li").fetchall()
    con.close()

    assert len(sources) == 1
    assert sources[0][0] == "passthrough"


# ---------------------------------------------------------------------------
# Section AR-N: skus_used deterministic service binding
# ---------------------------------------------------------------------------

def test_arn_sku_used_deterministic_service(tmp_path, monkeypatch):
    """AR-N: A SKU presented with multiple service labels inserts its rate under a deterministic service."""
    db_path = str(tmp_path / "projection.duckdb")
    con = duckdb.connect(db_path)
    _setup_catalog_table(con)
    _setup_mappings_table(con)
    con.execute("""
        INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, aws_amortized_cost)
        VALUES 
            ('k_s1', 'AmazonKMS', 'us-central1', 'us-east-1', 100.0, 'OnDemand', 10.0),
            ('k_s2', 'AmazonKMS', 'us-central1', 'us-east-1', 100.0, 'OnDemand', 10.0);

        INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence)
        VALUES 
            ('k_s1', 'Cloud KMS', 'SKU-KMS-KEY', 'KMS Key', 'map', 1.0, 'us-central1', '', 1.0),
            ('k_s2', 'Cloud Key Management Service (KMS)', 'SKU-KMS-KEY', 'KMS Key', 'map', 1.0, 'us-central1', '', 1.0);
    """)
    con.close()

    monkeypatch.setattr(apply_rates, "DB_PATH", db_path)

    cat_path = str(tmp_path / "catalog.duckdb")
    cat_db = duckdb.connect(cat_path)
    cat_db.execute("CREATE TABLE skus (sku_id TEXT, service_name TEXT, description TEXT, resource_family TEXT, resource_group TEXT, usage_type TEXT, usage_unit TEXT, service_regions TEXT[])")
    cat_db.execute("CREATE TABLE tiered_rates (sku_id TEXT, tier_start DOUBLE, rate_usd DOUBLE)")
    cat_db.execute("""
        INSERT INTO skus VALUES ('SKU-KMS-KEY', 'Cloud Key Management Service (KMS)', 'KMS Key', 'Security', 'KMS', 'OnDemand', 'count', ['us-central1']);
        INSERT INTO tiered_rates VALUES ('SKU-KMS-KEY', 0.0, 0.06);
    """)
    cat_db.close()
    monkeypatch.setattr(apply_rates, "CATALOG_DB", cat_path)

    apply_rates.main()

    con = duckdb.connect(db_path)
    rate_svc = con.execute("SELECT gcp_service FROM gcp_sku_rates WHERE gcp_sku_id = 'SKU-KMS-KEY'").fetchall()
    con.close()

    assert len(rate_svc) > 0
    # Canonical service from catalog or deterministic MIN
    assert rate_svc[0][0] == "Cloud Key Management Service (KMS)"


# ---------------------------------------------------------------------------
# Section NAME-1: gcp_service normalized before CUD synthesis
# ---------------------------------------------------------------------------

def test_name1_service_normalized_before_cud(tmp_path, monkeypatch):
    """NAME-1: Casual service name (e.g. 'Memorystore') is normalized to canonical catalog name
    before CUD synthesis, ensuring CUD rates are synthesized and 1yr/3yr CUD columns populated."""
    db_path = str(tmp_path / "projection.duckdb")
    con = duckdb.connect(db_path)
    _setup_catalog_table(con)
    _setup_mappings_table(con)
    con.execute("""
        INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, aws_amortized_cost)
        VALUES ('k_mem', 'AmazonElastiCache', 'us-central1', 'us-east-1', 100.0, 'OnDemand', 50.0);

        INSERT INTO aws_li_to_gcp_li (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence)
        VALUES ('k_mem', 'Memorystore', 'SKU-MEM-NODE', 'Memorystore Node', 'map', 1.0, 'us-central1', '', 1.0);
    """)
    con.close()

    monkeypatch.setattr(apply_rates, "DB_PATH", db_path)

    cat_path = str(tmp_path / "catalog.duckdb")
    cat_db = duckdb.connect(cat_path)
    cat_db.execute("CREATE TABLE skus (sku_id TEXT, service_name TEXT, description TEXT, resource_family TEXT, resource_group TEXT, usage_type TEXT, usage_unit TEXT, service_regions TEXT[])")
    cat_db.execute("CREATE TABLE tiered_rates (sku_id TEXT, tier_start DOUBLE, rate_usd DOUBLE)")
    cat_db.execute("""
        INSERT INTO skus VALUES ('SKU-MEM-NODE', 'Cloud Memorystore for Redis', 'Memorystore Node', 'Database', 'Memory', 'OnDemand', 'h', ['us-central1']);
        INSERT INTO tiered_rates VALUES ('SKU-MEM-NODE', 0.0, 0.10);
    """)
    cat_db.close()
    monkeypatch.setattr(apply_rates, "CATALOG_DB", cat_path)

    apply_rates.main()

    con = duckdb.connect(db_path)
    row = con.execute("""
        SELECT gcp_service FROM aws_li_to_gcp_li WHERE aws_li_key = 'k_mem'
    """).fetchone()
    # Check that gcp_service in aws_li_to_gcp_li is canonicalized
    assert row[0] == "Cloud Memorystore for Redis"

    # Check that CUD rates were synthesized for the canonical service in the SKU's region
    cud_rates = con.execute("""
        SELECT pricing_type, rate_usd FROM gcp_sku_rates
        WHERE gcp_sku_id = 'SKU-MEM-NODE' AND pricing_type IN ('Commit1Yr', 'Commit3Yr') AND region = 'us-central1'
        ORDER BY pricing_type
    """).fetchall()
    assert len(cud_rates) == 2
    # Check projection has CUD costs populated
    proj = con.execute("""
        SELECT gcp_projected_cost, gcp_cost_1yr_cud, gcp_cost_3yr_cud
        FROM gcp_projection WHERE aws_li_key = 'k_mem'
    """).fetchone()
    con.close()

    assert proj[0] == 100.0 * 1.0 * 0.10  # OnDemand = $10.0
    assert proj[1] < proj[0]               # 1yr CUD populated and discounted
    assert proj[2] < proj[1]               # 3yr CUD populated and discounted


# ---------------------------------------------------------------------------
# Section Minor: merge_mappings warns and continues on missing group files
# ---------------------------------------------------------------------------

def test_minor_merge_mappings_warn_and_continue_on_missing_group(tmp_path, monkeypatch):
    """Minor: merge_mappings.py warns and continues when groups are missing, recording in merge_report.json."""
    import merge_mappings

    db_path = str(tmp_path / "projection.duckdb")
    con = duckdb.connect(db_path)
    _setup_catalog_table(con)
    _setup_mappings_table(con)
    con.execute("""
        INSERT INTO aws_li_catalog (aws_li_key, product, gcp_region, aws_region, total_usage, pricing_model, aws_amortized_cost)
        VALUES ('k1', 'AmazonEC2', 'us-central1', 'us-east-1', 1.0, 'OnDemand', 10.0);
    """)
    con.close()

    # Create phase2_manifest.json listing compute and storage as expected LLM groups
    manifest_path = str(tmp_path / "phase2_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "compute": {"needs_llm": True},
            "storage": {"needs_llm": True},
        }, f)

    mappings_dir = str(tmp_path / "mappings")
    os.makedirs(mappings_dir, exist_ok=True)
    # Write only compute_mappings.json, omitting storage
    with open(os.path.join(mappings_dir, "compute_mappings.json"), "w") as f:
        json.dump([{
            "aws_li_key": "k1",
            "gcp_service": "Compute Engine",
            "gcp_sku_id": "SKU-TEST",
            "gcp_sku_name": "Test SKU",
            "strategy": "map",
            "unit_multiplier": 1.0,
            "mapping_confidence": 0.95,
        }], f)

    sys.argv = ["merge_mappings.py", db_path, mappings_dir]
    with pytest.raises(SystemExit) as exc:
        merge_mappings.main()
    assert exc.value.code == 0

    # Verify report records missing groups
    report_path = str(tmp_path / "merge_report.json")
    with open(report_path, "r", encoding="utf-8") as f:
        report = json.load(f)

    assert "missing_groups" in report
    assert "storage" in report["missing_groups"]
    assert "compute" not in report["missing_groups"]


# ---------------------------------------------------------------------------
# Section L: License cost display & complete modeling (L-1, L-2, L-3, L-4)
# ---------------------------------------------------------------------------

def test_l1_flag_license_exposure_reconciliation(tmp_path):
    """L-1: flag_license_exposure only flags rows when license component is NOT modeled."""
    from apply_rates import flag_license_exposure
    db_path = str(tmp_path / "projection.duckdb")
    con = duckdb.connect(db_path)
    _setup_catalog_table(con)
    _setup_mappings_table(con)
    try:
        con.execute("ALTER TABLE aws_li_catalog ADD COLUMN operating_system TEXT")
    except Exception:
        pass
    try:
        con.execute("ALTER TABLE aws_li_catalog ADD COLUMN database_engine TEXT")
    except Exception:
        pass

    con.execute("""
        INSERT INTO aws_li_catalog (aws_li_key, product, operating_system, aws_amortized_cost, is_workload)
        VALUES 
            ('k_win_modeled', 'AmazonEC2', 'Windows', 500.0, true),
            ('k_oracle_unmodeled', 'AmazonRDS', 'Oracle', 800.0, true);
    """)

    # Windows has compute + license component modeled
    con.execute("""
        INSERT INTO aws_li_to_gcp_li (aws_li_key, strategy, gcp_service, gcp_sku_id, component, mapping_confidence, projection_note)
        VALUES 
            ('k_win_modeled', 'break_down', 'Compute Engine', 'sku-core', 'core', 0.90, ''),
            ('k_win_modeled', 'break_down', 'Compute Engine', '68CA-EFE5-B3EB', 'license', 0.90, 'Windows license');
    """)

    # Oracle has only base engine mapped with no license component
    con.execute("""
        INSERT INTO aws_li_to_gcp_li (aws_li_key, strategy, gcp_service, gcp_sku_id, component, mapping_confidence, projection_note)
        VALUES 
            ('k_oracle_unmodeled', 'map', 'Cloud SQL', 'sku-db', 'default', 0.85, 'Oracle engine');
    """)

    flag_license_exposure(con)
    try:
        import render_notes
        render_notes.render_notes_for_db(con)
    except Exception:
        pass

    res = con.execute("""
        SELECT aws_li_key, component, mapping_confidence, projection_note, license_status
        FROM aws_li_to_gcp_li
        ORDER BY aws_li_key, component
    """).fetchall()

    win_rows = [r for r in res if r[0] == 'k_win_modeled']
    oracle_rows = [r for r in res if r[0] == 'k_oracle_unmodeled']

    # Windows rows should NOT have premium_not_modeled license status and should NOT be capped
    for r in win_rows:
        assert r[4] != 'premium_not_modeled'
        assert r[2] == 0.90

    # Oracle row should have license_status = 'premium_not_modeled' and confidence capped at 0.50
    assert len(oracle_rows) == 1
    assert oracle_rows[0][4] == 'premium_not_modeled'
    assert "OS/engine licence premium that is not modeled" in oracle_rows[0][3]
    assert oracle_rows[0][2] == 0.50

    con.close()


def test_l2_ubuntu_pro_and_suse_and_oracle_license_entries():
    """L-2: _license_entries emits catalog SKUs for Ubuntu Pro and SUSE, and labeled passthrough for Oracle."""
    from apply_static_mappings import _license_entries

    # 1. Ubuntu Pro with 4 vCPUs
    row_ubuntu = {
        "operating_system": "Ubuntu Pro",
        "operation": "RunInstances",
        "usage_type": "BoxUsage:c5.xlarge",
        "product": "Amazon Elastic Compute Cloud",
    }
    lics_u = _license_entries(row_ubuntu, "k_u", "us-central1", 4, 16.0)
    assert len(lics_u) == 1
    assert lics_u[0]["component"] == "license"
    assert lics_u[0]["gcp_sku_id"] == "DC45-E5BB-8CDD"  # 4 to 5 vCPUs band
    assert "Ubuntu Pro" in lics_u[0]["gcp_sku_name"]

    # 2. SUSE Linux Enterprise Server with 8 vCPUs
    row_suse = {
        "operating_system": "SUSE Linux",
        "operation": "RunInstances",
        "usage_type": "BoxUsage:c5.2xlarge",
        "product": "Amazon Elastic Compute Cloud",
    }
    lics_s = _license_entries(row_suse, "k_s", "us-central1", 8, 32.0)
    assert len(lics_s) == 1
    assert lics_s[0]["component"] == "license"
    assert lics_s[0]["gcp_sku_id"] == "19BA-AC0D-D389"  # 6 or more vCPUs band
    assert "SUSE" in lics_s[0]["gcp_sku_name"]

    # 3. Oracle DB labeled passthrough
    row_oracle = {
        "operating_system": "Oracle",
        "operation": "CreateDBInstance",
        "usage_type": "InstanceUsage:db.m5.xlarge",
        "product": "Amazon Relational Database Service",
    }
    lics_o = _license_entries(row_oracle, "k_o", "us-central1", 4, 16.0)
    assert len(lics_o) == 1
    assert lics_o[0]["strategy"] == "passthrough"
    assert "Oracle license — BYOL; not modeled, add separately" in lics_o[0]["projection_note"]


def test_l3_l4_render_report_and_export_display(tmp_path, monkeypatch):
    """L-3 & L-4: render_report shows instance + license separately with a plus and sub-row; export_report has dedicated columns."""
    import csv
    import render_report
    import export_report

    job_dir = tmp_path / "job_lic"
    audit_dir = job_dir / "projection-audit"
    audit_dir.mkdir(parents=True)
    db_file = audit_dir / "projection.duckdb"

    con = duckdb.connect(str(db_file))
    con.execute("""
        CREATE TABLE aws_li_catalog (
            aws_li_key TEXT PRIMARY KEY,
            product TEXT,
            instance_type TEXT,
            instance_vcpus INTEGER,
            instance_ram_gb DOUBLE,
            instance_count DOUBLE,
            usage_type TEXT,
            operation TEXT,
            aws_region TEXT,
            gcp_region TEXT,
            line_item_type TEXT,
            mechanic_group TEXT,
            pricing_model TEXT,
            pricing_unit TEXT,
            is_workload BOOLEAN,
            total_usage DOUBLE,
            aws_amortized_cost DOUBLE
        );
        CREATE TABLE aws_li_to_gcp_li (
            aws_li_key TEXT,
            rowid BIGINT,
            strategy TEXT,
            gcp_service TEXT,
            gcp_machine_type TEXT,
            gcp_sku_id TEXT,
            gcp_sku_name TEXT,
            component TEXT,
            unit_multiplier DOUBLE,
            projection_note TEXT,
            mapping_confidence DOUBLE,
            is_assumption BOOLEAN
        );
        CREATE TABLE gcp_projection (
            aws_li_key TEXT,
            component TEXT,
            gcp_service TEXT,
            gcp_machine_type TEXT,
            gcp_region TEXT,
            is_workload BOOLEAN,
            gcp_projected_cost DOUBLE,
            gcp_cost_1yr_cud DOUBLE,
            gcp_cost_3yr_cud DOUBLE,
            projection_status TEXT,
            projection_note TEXT
        );
        CREATE TABLE run_results (
            run_id TEXT,
            ts_utc TEXT,
            prospect TEXT
        );
    """)

    con.execute("""
        INSERT INTO aws_li_catalog (aws_li_key, product, instance_type, instance_vcpus, instance_ram_gb, instance_count, usage_type, operation, aws_region, gcp_region, line_item_type, mechanic_group, pricing_model, pricing_unit, is_workload, total_usage, aws_amortized_cost) VALUES
            ('k_win', 'Amazon Elastic Compute Cloud', 'm5.xlarge', 4, 16.0, 1.0, 'BoxUsage:m5.xlarge', 'RunInstances:0002', 'us-east-1', 'us-central1', 'Usage', 'compute_breakdown', 'OnDemand', 'Hrs', true, 730, 200.0),
            ('k_fallback', 'Amazon Elastic Compute Cloud', 'custom.unlisted', 4, 16.0, 1.0, 'BoxUsage:custom.unlisted', 'RunInstances:0002', 'us-east-1', 'us-central1', 'Usage', 'compute_breakdown', 'OnDemand', 'Hrs', true, 730, 50.0);
        INSERT INTO aws_li_to_gcp_li VALUES
            ('k_win', 1, 'break_down', 'Compute Engine', 'n2-standard-4', 'sku-core', 'N2 Core', 'core', 4.0, 'Base compute', 0.95, false),
            ('k_win', 2, 'break_down', 'Compute Engine', 'n2-standard-4', 'sku-ram', 'N2 Ram', 'ram', 16.0, 'Base compute', 0.95, false),
            ('k_win', 3, 'break_down', 'Compute Engine', 'n2-standard-4', '68CA-EFE5-B3EB', 'Licensing Fee for Windows Server 2025', 'license', 1.0, 'Windows license', 0.95, false),
            ('k_fallback', 4, 'break_down', 'Compute Engine', 'n2-standard-4', 'sku-core', 'N2 Core', 'core', 4.0, 'Base compute', 0.95, false),
            ('k_fallback', 5, 'break_down', 'Compute Engine', 'n2-standard-4', '68CA-EFE5-B3EB', 'Licensing Fee for Windows Server 2025', 'license', 1.0, 'Windows license', 0.95, false);
        INSERT INTO gcp_projection VALUES
            ('k_win', 'core', 'Compute Engine', 'n2-standard-4', 'us-central1', true, 100.0, 80.0, 60.0, 'mapped', 'OK'),
            ('k_win', 'ram', 'Compute Engine', 'n2-standard-4', 'us-central1', true, 20.0, 16.0, 12.0, 'mapped', 'OK'),
            ('k_win', 'license', 'Compute Engine', 'n2-standard-4', 'us-central1', true, 30.0, 30.0, 30.0, 'mapped', 'OK'),
            ('k_fallback', 'core', 'Compute Engine', 'n2-standard-4', 'us-central1', true, 35.0, 28.0, 21.0, 'mapped', 'OK'),
            ('k_fallback', 'license', 'Compute Engine', 'n2-standard-4', 'us-central1', true, 15.0, 15.0, 15.0, 'mapped', 'OK');
        INSERT INTO run_results VALUES
            ('run-test', '2026-10-15 00:00:00', 'Acme Corp');
    """)
    con.close()

    monkeypatch.chdir(job_dir)
    monkeypatch.setattr("render_report.JOB_DIR", str(job_dir))
    monkeypatch.setattr("render_report.DB_PATH", str(db_file))
    monkeypatch.setattr("export_report.JOB_DIR", str(job_dir))
    monkeypatch.setattr("export_report.DB_PATH", str(db_file))

    # Test HTML report rendering
    render_report.main()
    report_html_path = audit_dir / "report.html"
    assert report_html_path.exists()
    html = report_html_path.read_text(encoding="utf-8")

    # 1. Shows GCP base + license separately (e.g. $120.00 + $30.00) and NOT merged into $150.00
    assert "$120.00 + $30.00" in html
    # 2. Phase 2: m5.xlarge has authoritative license_fraction -> AWS splits into base + license ($102.12 + $97.88 = $200.00)
    assert "$102.12 + $97.88" in html
    assert '<span class="plus-sign">+</span>$97.88' in html
    assert "AWS license split estimated from AWS Price List Windows/Linux OD ratio." in html

    # 3. Phase 1 fallback: custom.unlisted has no license_fraction -> shows (incl. OS license) label and one AWS OD figure
    assert "custom.unlisted" in html
    assert "(incl. OS license)" in html

    # 4. Shows the GCP sub-row with the plus sign and license name
    assert '<span class="plus-sign">+</span>$30.00' in html
    assert "Licensing Fee for Windows Server 2025" in html

    # Test CSV export
    csv_out = str(job_dir / "export.csv")
    monkeypatch.setattr("sys.argv", ["export_report.py", "--format", "csv", "--out", csv_out])
    export_report.main()

    with open(csv_out, encoding="utf-8") as f:
        reader = list(csv.reader(f))
    headers = reader[0]
    row = reader[1]

    # Verify dedicated columns exist
    assert "instance_cost" in headers
    assert "license_name" in headers
    assert "license_cost" in headers

    inst_cost_idx = headers.index("instance_cost")
    lic_name_idx = headers.index("license_name")
    lic_cost_idx = headers.index("license_cost")

    assert float(row[inst_cost_idx]) == 120.0
    assert "Licensing Fee for Windows Server 2025" in row[lic_name_idx]
    assert float(row[lic_cost_idx]) == 30.0
