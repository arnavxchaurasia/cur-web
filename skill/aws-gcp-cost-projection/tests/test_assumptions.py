#!/usr/bin/env python3
"""tests/test_assumptions.py — Test load_assumptions() from config_loader."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from config_loader import load_assumptions  # noqa: E402


def test_load_assumptions_returns_snapshot_when_file_exists():
    """load_assumptions(job_dir) reads values from <job_dir>/assumptions.json."""
    with tempfile.TemporaryDirectory() as job_dir:
        snapshot = {
            "version": "abc123456789",
            "generated_at": "2026-10-21T09:00:00Z",
            "values": {
                "glue_dpu_vcpu": 8.0,
                "glue_dpu_ram_gib": 24.0,
                "looker_studio_pro_seat_usd": 12.0,
                "aurora_acu_vcpu": 3.0,
                "aurora_acu_ram_gib": 24.0,
                "cognito_requests_per_mau": 25.0,
            },
        }
        with open(os.path.join(job_dir, "assumptions.json"), "w") as f:
            json.dump(snapshot, f)

        result = load_assumptions(job_dir)
        assert result["glue_dpu_vcpu"] == 8.0
        assert result["glue_dpu_ram_gib"] == 24.0
        assert result["looker_studio_pro_seat_usd"] == 12.0
        assert result["aurora_acu_vcpu"] == 3.0
        assert result["aurora_acu_ram_gib"] == 24.0
        assert result["cognito_requests_per_mau"] == 25.0


def test_load_assumptions_returns_defaults_when_file_absent():
    """load_assumptions() falls back to committed defaults when no snapshot."""
    result = load_assumptions(None)
    # These must match the code's original hardcoded constants
    assert result["glue_dpu_vcpu"] == 4.0
    assert result["glue_dpu_ram_gib"] == 16.0
    assert result["looker_studio_pro_seat_usd"] == 9.0
    assert result["aurora_acu_vcpu"] == 2.0
    assert result["aurora_acu_ram_gib"] == 16.0
    assert "cognito_requests_per_mau" not in result


def test_load_assumptions_returns_defaults_on_malformed_json():
    """load_assumptions(job_dir) never raises — returns defaults on bad JSON."""
    with tempfile.TemporaryDirectory() as job_dir:
        # Write malformed JSON
        with open(os.path.join(job_dir, "assumptions.json"), "w") as f:
            f.write("{not valid json")

        result = load_assumptions(job_dir)
        # Should fall back to defaults
        assert result["glue_dpu_vcpu"] == 4.0
        assert result["looker_studio_pro_seat_usd"] == 9.0


def test_load_assumptions_returns_defaults_on_empty_file():
    """load_assumptions(job_dir) handles empty/missing values gracefully."""
    with tempfile.TemporaryDirectory() as job_dir:
        with open(os.path.join(job_dir, "assumptions.json"), "w") as f:
            json.dump({"version": "x", "values": {}}, f)

        result = load_assumptions(job_dir)
        assert result["glue_dpu_vcpu"] == 4.0


def test_every_default_key_referenced_in_scripts():
    """Ratchet: every key in assumption-defaults.json must be referenced in scripts/*.py."""
    data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    defaults_path = os.path.join(data_dir, "assumption-defaults.json")
    with open(defaults_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    keys = list(data.get("values", {}).keys())
    assert keys, "assumption-defaults.json has no keys"

    scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
    script_files = [
        os.path.join(scripts_dir, fname)
        for fname in os.listdir(scripts_dir)
        if fname.endswith(".py")
    ]

    all_script_text = ""
    for sf in script_files:
        with open(sf, "r", encoding="utf-8", errors="replace") as f:
            all_script_text += f.read() + "\n"

    unreferenced = [k for k in keys if k not in all_script_text]
    assert not unreferenced, (
        f"Found unreferenced assumption keys in data/assumption-defaults.json: {unreferenced}. "
        "Every default key must be wired to at least one reference in scripts/*.py."
    )


def test_load_assumptions_rejects_out_of_band_ratio():
    """load_assumptions(job_dir) falls back to defaults when stored ratio is out of band."""
    with tempfile.TemporaryDirectory() as job_dir:
        # 0.1 vCPU with 64 GiB RAM is 1:640 — not in allowed ratios {4, 8, 16}
        snapshot = {
            "version": "bad_ratio",
            "values": {
                "aurora_acu_vcpu": 0.1,
                "aurora_acu_ram_gib": 64.0,
            },
        }
        with open(os.path.join(job_dir, "assumptions.json"), "w") as f:
            json.dump(snapshot, f)

        result = load_assumptions(job_dir)
        # Must fall back to default values (2.0 and 16.0 -> 1:8)
        assert result["aurora_acu_vcpu"] == 2.0
        assert result["aurora_acu_ram_gib"] == 16.0


if __name__ == "__main__":
    test_load_assumptions_returns_snapshot_when_file_exists()
    test_load_assumptions_returns_defaults_when_file_absent()
    test_load_assumptions_returns_defaults_on_malformed_json()
    test_load_assumptions_returns_defaults_on_empty_file()
    test_load_assumptions_rejects_out_of_band_ratio()
    test_every_default_key_referenced_in_scripts()
    print("All tests passed!")