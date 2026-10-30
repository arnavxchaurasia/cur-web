import os
import json
import duckdb
import pytest
from scripts.advisory_reviewer import run_advisory_review
from scripts.refresh_store import (
    load_refresh_store,
    save_refresh_store,
    record_or_promote_mapping,
    get_trusted_mapping,
)


def test_advisory_reviewer_findings(tmp_path):
    job_dir = tmp_path / "job_test"
    audit_dir = job_dir / "projection-audit"
    audit_dir.mkdir(parents=True)
    db_path = audit_dir / "projection.duckdb"

    con = duckdb.connect(str(db_path))
    con.execute("""
        CREATE TABLE aws_li_catalog (
            aws_li_key TEXT,
            product TEXT,
            usage_type TEXT,
            pricing_unit TEXT,
            aws_amortized_cost REAL,
            aws_od_cost REAL,
            is_workload BOOLEAN,
            instance_type TEXT
        );
        CREATE TABLE gcp_projection (
            aws_li_key TEXT,
            gcp_service TEXT,
            gcp_sku_name TEXT,
            gcp_sku_id TEXT,
            strategy TEXT,
            unit_multiplier REAL,
            gcp_projected_cost REAL,
            gcp_od_cost REAL,
            projection_note TEXT
        );
    """)

    # 1. suspect_ratio: ratio 10.0x (>3.0) on $100
    con.execute("""
        INSERT INTO aws_li_catalog VALUES (
            'k1', 'Amazon Elastic Compute Cloud', 'BoxUsage:c5.xlarge', 'Hrs', 100.0, 100.0, TRUE, 'c5.xlarge'
        );
        INSERT INTO gcp_projection VALUES (
            'k1', 'Compute Engine', 'N2 Instance Core', 'sku-1', 'map', 4.0, 1000.0, 1000.0, 'mapped'
        );
    """)

    # 2. target_not_in_allowed: target is an AWS echo
    con.execute("""
        INSERT INTO aws_li_catalog VALUES (
            'k2', 'Amazon Kinesis', 'DataStreams', 'GB', 20.0, 20.0, TRUE, NULL
        );
        INSERT INTO gcp_projection VALUES (
            'k2', 'Amazon Kinesis', 'Kinesis Data Streams', 'sku-2', 'map', 1.0, 20.0, 20.0, 'echo'
        );
    """)

    # 3. passthrough_should_be_mapped: EC2 left as unreviewed passthrough
    con.execute("""
        INSERT INTO aws_li_catalog VALUES (
            'k3', 'Amazon Simple Storage Service', 'TimedStorage-ByteHrs', 'GB-Mo', 50.0, 50.0, TRUE, NULL
        );
        INSERT INTO gcp_projection VALUES (
            'k3', 'Cloud Storage', NULL, NULL, 'passthrough', 1.0, 50.0, 50.0, 'unreviewed'
        );
    """)

    con.close()

    findings = run_advisory_review(str(job_dir))
    assert len(findings) >= 3

    types = {f["finding_type"] for f in findings}
    assert "suspect_ratio" in types
    assert "target_not_in_allowed" in types
    assert "passthrough_should_be_mapped" in types

    out_file = audit_dir / "review_findings.json"
    assert out_file.exists()
    with open(out_file) as f:
        data = json.load(f)
    assert len(data) == len(findings)


def test_refresh_store_promotion(tmp_path):
    store_file = tmp_path / "test_store.json"

    # 1. Initial provisional record
    entry1 = record_or_promote_mapping(
        key="custom_service_xyz",
        gcp_service="Cloud Run",
        gcp_sku_name="Cloud Run Requests",
        strategy="map",
        unit_multiplier=1.0,
        source="job_1",
        admin_accepted=False,
        store_path=str(store_file),
    )
    assert entry1["status"] == "provisional"
    assert entry1["confirmation_count"] == 1
    assert get_trusted_mapping("custom_service_xyz", store_path=str(store_file)) is None

    # 2. Second independent confirmation -> promoted to trusted
    entry2 = record_or_promote_mapping(
        key="custom_service_xyz",
        gcp_service="Cloud Run",
        gcp_sku_name="Cloud Run Requests",
        strategy="map",
        unit_multiplier=1.0,
        source="job_2",
        admin_accepted=False,
        store_path=str(store_file),
    )
    assert entry2["status"] == "trusted"
    assert entry2["confirmation_count"] == 2
    trusted = get_trusted_mapping("custom_service_xyz", store_path=str(store_file))
    assert trusted is not None
    assert trusted["gcp_service"] == "Cloud Run"

    # 3. Direct admin accept -> immediately trusted
    entry3 = record_or_promote_mapping(
        key="fast_promoted_svc",
        gcp_service="BigQuery",
        gcp_sku_name="Analysis",
        strategy="map",
        source="admin",
        admin_accepted=True,
        store_path=str(store_file),
    )
    assert entry3["status"] == "trusted"
    assert get_trusted_mapping("fast_promoted_svc", store_path=str(store_file)) is not None


def test_refresh_store_repeated_same_source_does_not_promote(tmp_path):
    """Independence gate (review F2): recording the SAME source repeatedly (e.g.
    re-running the reviewer on one bill) must NOT inflate confirmation_count or
    auto-promote. Promotion requires distinct sources or an admin accept."""
    store_file = tmp_path / "indep_store.json"
    kw = dict(key="svc_repeat", gcp_service="Cloud Run", strategy="map",
              admin_accepted=False, store_path=str(store_file))

    e1 = record_or_promote_mapping(source="job_1", **kw)
    assert e1["confirmation_count"] == 1 and e1["status"] == "provisional"

    # Same source again -> still 1, still provisional (not independent evidence).
    e2 = record_or_promote_mapping(source="job_1", **kw)
    assert e2["confirmation_count"] == 1, "repeated same source inflated the count"
    assert e2["status"] == "provisional"
    assert get_trusted_mapping("svc_repeat", store_path=str(store_file)) is None

    # A genuinely different source reaches the threshold -> trusted.
    e3 = record_or_promote_mapping(source="job_2", **kw)
    assert e3["confirmation_count"] == 2 and e3["status"] == "trusted"


def test_closed_loop_trusted_mapping_applies_automatically(tmp_path, monkeypatch):
    """
    Demonstrate closed loop:
    When a mapping is trusted in refresh_store.json, classify_mechanics automatically
    emits it into mappings/refresh_mappings.json and prunes it from LLM manifest!
    """
    import subprocess
    import sys
    from scripts.refresh_store import record_or_promote_mapping

    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()
    fake_store_path = fake_data_dir / "refresh_store.json"

    # Promote a service to trusted via admin acceptance
    record_or_promote_mapping(
        key="custom_nlp_tool",
        gcp_service="Natural Language API",
        gcp_sku_name="Content Classification",
        strategy="map",
        unit_multiplier=1.0,
        source="admin@example.com",
        admin_accepted=True,
        store_path=str(fake_store_path),
    )

    # Point SKILL_DIR to tmp_path
    monkeypatch.setenv("SKILL_DIR", str(tmp_path))

    # Set up a test DuckDB with custom_nlp_tool in aws_li_catalog
    job_dir = tmp_path / "job_run"
    job_dir.mkdir()
    db_path = job_dir / "projection.duckdb"

    con = duckdb.connect(str(db_path))
    con.execute("""
        CREATE TABLE aws_li_catalog (
            aws_li_key TEXT PRIMARY KEY,
            product TEXT,
            usage_type TEXT,
            operation TEXT,
            pricing_unit TEXT,
            line_item_type TEXT,
            pricing_model TEXT,
            aws_amortized_cost REAL,
            instance_type TEXT,
            instance_vcpus REAL,
            instance_ram_gb REAL,
            instance_count REAL,
            is_workload BOOLEAN,
            aws_region TEXT,
            gcp_region TEXT
        );
        INSERT INTO aws_li_catalog VALUES (
            'nlp_row_1', 'Amazon Custom NLP Tool', 'ClassificationUnits', 'Classify',
            'Units', 'Usage', 'OnDemand', 75.0, NULL, NULL, NULL, 1, TRUE, 'us-east-1', 'us-east1'
        );
    """)
    con.close()

    # Run classify_mechanics
    from scripts.classify_mechanics import main as classify_main
    monkeypatch.setattr(sys, "argv", ["classify_mechanics.py", str(db_path)])

    try:
        classify_main()
    except SystemExit as e:
        assert e.code == 0

    # Verify refresh_mappings.json was written with the trusted mapping
    rf_mappings_file = job_dir / "mappings" / "refresh_mappings.json"
    assert rf_mappings_file.exists()
    with open(rf_mappings_file) as f:
        rf_data = json.load(f)

    assert len(rf_data) == 1
    assert rf_data[0]["aws_li_key"] == "nlp_row_1"
    assert rf_data[0]["gcp_service"] == "Natural Language API"
    assert rf_data[0]["decided_by"] == "refresh_store"

    # Verify manifest misc rows is 0 (it was short-circuited and not sent to LLM!)
    manifest_file = job_dir / "phase2_manifest.json"
    assert manifest_file.exists()
    with open(manifest_file) as f:
        manifest = json.load(f)

    misc_group = manifest.get("misc", {})
    assert misc_group.get("row_count", 0) == 0

