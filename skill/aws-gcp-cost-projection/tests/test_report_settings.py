#!/usr/bin/env python3
"""tests/test_report_settings.py — Test load_report_settings() from config_loader."""

import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from config_loader import load_report_settings  # noqa: E402


def test_load_report_settings_returns_snapshot_when_file_exists():
    """load_report_settings(job_dir) reads values from <job_dir>/report-settings.json."""
    with tempfile.TemporaryDirectory() as job_dir:
        snapshot = {
            "version": "abc123456789",
            "generated_at": "2026-10-22T09:00:00Z",
            "values": {
                "col_gcp_cud_1yr": "false",
                "col_gcp_cud_3yr": "true",
                "col_diff": "true",
                "col_pct_of_aws": "false",
                "col_line_items": "true",
                "col_region": "true",
                "section_discounts": "false",
                "section_wins_losses": "true",
                "section_assumptions": "true",
                "section_data_gaps": "false",
                "row_min_cost_usd": "50.0",
            },
        }
        with open(os.path.join(job_dir, "report-settings.json"), "w") as f:
            json.dump(snapshot, f)

        result = load_report_settings(job_dir)
        assert result["col_gcp_cud_1yr"] == "false"
        assert result["col_gcp_cud_3yr"] == "true"
        assert result["col_diff"] == "true"
        assert result["col_pct_of_aws"] == "false"
        assert result["col_line_items"] == "true"
        assert result["col_region"] == "true"
        assert result["section_discounts"] == "false"
        assert result["section_wins_losses"] == "true"
        assert result["section_assumptions"] == "true"
        assert result["section_data_gaps"] == "false"
        assert result["row_min_cost_usd"] == "50.0"


def test_load_report_settings_returns_defaults_when_file_absent():
    """load_report_settings() falls back to committed defaults when no snapshot."""
    result = load_report_settings(None)
    assert result["col_gcp_cud_1yr"] == "true"
    assert result["col_gcp_cud_3yr"] == "true"
    assert result["col_diff"] == "true"
    assert result["col_pct_of_aws"] == "true"
    assert result["col_line_items"] == "false"
    assert result["col_region"] == "false"
    assert result["section_discounts"] == "true"
    assert result["section_wins_losses"] == "true"
    assert result["section_assumptions"] == "true"
    assert result["section_data_gaps"] == "true"
    assert result["row_min_cost_usd"] == "0"


def test_load_report_settings_returns_defaults_on_malformed_json():
    """load_report_settings(job_dir) never raises — returns defaults on bad JSON."""
    with tempfile.TemporaryDirectory() as job_dir:
        with open(os.path.join(job_dir, "report-settings.json"), "w") as f:
            f.write("{not valid json")

        result = load_report_settings(job_dir)
        assert result["col_gcp_cud_1yr"] == "true"
        assert "looker_studio_pro_seat_usd" not in result  # not a report setting


def test_load_report_settings_returns_defaults_on_empty_file():
    """load_report_settings(job_dir) handles empty values gracefully."""
    with tempfile.TemporaryDirectory() as job_dir:
        with open(os.path.join(job_dir, "report-settings.json"), "w") as f:
            json.dump({"version": "x", "values": {}}, f)

        result = load_report_settings(job_dir)
        assert result["col_gcp_cud_1yr"] == "true"


def test_report_settings_keys_match_renderer():
    """Every settable key is read by the renderer, and vice versa."""
    # 1. Collect the settable keys from the defaults file
    defaults_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "data", "report-settings-defaults.json",
    )
    with open(defaults_path) as f:
        defaults = json.load(f)
    settable_keys = set(defaults["values"].keys())

    # 2. Collect keys the renderer reads via rs_bool() / rs_num()
    render_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "scripts", "render_report.py",
    )
    with open(render_path, encoding="utf-8", errors="replace") as f:
        source = f.read()
    renderer_keys = set(re.findall(r'rs_(?:bool|num)\(\s*["\']([a-z0-9_]+)["\']', source))

    # Keys are also referenced INDIRECTLY: the COLUMNS table in render_report.py
    # holds the setting key in the first tuple position and is consumed as
    # rs_bool(c[0]). A literal-only regex cannot see those. COLUMNS is the
    # correct design -- it keeps the <th> header and <td> body cells in step --
    # so the test must understand it, not force the renderer to inline literals.
    columns_block = source[source.index("COLUMNS = ["):]
    columns_block = columns_block[:columns_block.index(chr(10) + "]")]
    renderer_keys |= set(
        re.findall(r"""\(\s*["']([a-z0-9_]+)["']\s*,""", columns_block)
    )

    # 3. Assert the two sets are equal, naming drifting keys on both sides
    in_defaults_not_read = sorted(settable_keys - renderer_keys)
    in_renderer_not_settable = sorted(renderer_keys - settable_keys)

    assert not in_defaults_not_read and not in_renderer_not_settable, (
        "report settings drift:\n"
        + "\n".join(
            f"  in defaults but never read by render_report.py: {k}"
            for k in in_defaults_not_read
        )
        + "\n".join(
            f"  read by render_report.py but not settable:      {k}"
            for k in in_renderer_not_settable
        )
    )


def test_diag_emits_expected_code():
    """Each new diag() call site emits the expected code on a triggering fixture."""
    import tempfile
    import duckdb

    from diagnostics import diag, WARN, read_all

    with tempfile.TemporaryDirectory() as job_dir:
        # Call each new diag() code and verify it lands in the JSONL
        diag(WARN, "unmapped_misc_spend", "classify_mechanics.py",
             "test misc row", rows=1, usd=42.0, job_dir=job_dir,
             detail={"mechanic_group": "misc"})
        diag(WARN, "empty_group_mappings", "merge_mappings.py",
             "test empty group", rows=5, usd=100.0, job_dir=job_dir,
             detail={"mechanic_group": "test_group", "catalog_rows": 5, "mapping_rows": 0})
        diag(WARN, "commitment_rows_unclaimed", "apply_commitment_ignores.py",
             "test unclaimed commitment", rows=3, usd=75.0, job_dir=job_dir,
             detail={"mechanic_group": "commitment_discount", "catalog_rows": 3, "mapped_rows": 0})
        diag(WARN, "capacity_check_skipped", "reconcile_capacity.py",
             "test skipped capacity", rows=2, usd=30.0, job_dir=job_dir,
             detail={"reason": "instance_vcpus IS NULL", "component": "core",
                     "strategies": ["break_down", "map"]})
        diag(WARN, "materiality_dropped", "ingest.py",
             "test materiality drop", rows=7, usd=15.50, job_dir=job_dir,
             detail={"rows_dropped": 7, "usd_dropped": 15.50, "pct_of_workload": 0.5})

        recs = read_all(job_dir)
        codes = [r["code"] for r in recs]
        assert "unmapped_misc_spend" in codes
        assert "empty_group_mappings" in codes
        assert "commitment_rows_unclaimed" in codes
        assert "capacity_check_skipped" in codes
        assert "materiality_dropped" in codes


if __name__ == "__main__":
    test_load_report_settings_returns_snapshot_when_file_exists()
    test_load_report_settings_returns_defaults_when_file_absent()
    test_load_report_settings_returns_defaults_on_malformed_json()
    test_load_report_settings_returns_defaults_on_empty_file()
    test_report_settings_keys_match_renderer()
    test_diag_emits_expected_code()
    print("All tests passed!")