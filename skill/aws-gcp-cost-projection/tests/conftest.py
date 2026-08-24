"""
Shared fixtures for Gbill static mapper tests.

All tests use synthetic row dicts — no DuckDB required. Each fixture
represents a category of real CUR rows (EC2, S3, RDS, etc.) with the
minimum fields needed for classification and mapping.
"""

import sys, os
import pytest

# Allow imports from scripts/ without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


def row(**kwargs):
    """Build a minimal CUR row dict with sensible defaults."""
    defaults = {
        "aws_li_key":        "test-key-001",
        "product":           "",
        "usage_type":        "",
        "operation":         "",
        "unit":              "Hrs",
        "line_item_type":    "Usage",
        "pricing_model":     "OnDemand",
        "aws_amortized_cost": 100.0,
        "instance_type":     None,
        "instance_vcpus":    None,
        "instance_ram_gb":   None,
        "instance_count":    1,
        "billing_format":    None,
        "volume_type":       None,
        "gcp_region":        "us-central1",
        "total_usage":       1000.0,
    }
    defaults.update(kwargs)
    return defaults
