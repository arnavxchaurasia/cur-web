#!/usr/bin/env python3
"""Regenerate data/ratio_registry.json from candidate_pool.py.

The registry is the single source of truth for which vCPU:RAM ratios GCP
actually sells. Python reads it on the way out (config_loader) and Go reads it
on the way in (admin assumption writes), so neither side hardcodes the set.

Adding a GCP machine family to candidate_pool.py means re-running this script;
tests/test_assumptions.py fails until you do.
"""
import json
import os
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
DATA = os.path.join(os.path.dirname(SCRIPTS), "data")
REGISTRY_PATH = os.path.join(DATA, "ratio_registry.json")

# Assumption pairs whose quotient must be a shape GCP sells. Add a pair here
# when a new ratio group is introduced; both sides pick it up with no code
# change.
RATIO_GROUPS = [
    {"vcpu_key": "aurora_acu_vcpu", "ram_key": "aurora_acu_ram_gib", "label": "Aurora ACU"},
]


def build_registry() -> dict:
    from candidate_pool import get_allowed_ram_per_vcpu_ratios

    return {
        "_comment": (
            "Generated from scripts/candidate_pool.py ram_per_vcpu. "
            "Regenerate with scripts/gen_ratio_registry.py; "
            "tests/test_assumptions.py asserts it stays in sync."
        ),
        "allowed_ram_per_vcpu": sorted(get_allowed_ram_per_vcpu_ratios()),
        "ratio_groups": RATIO_GROUPS,
    }


def main() -> int:
    registry = build_registry()
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)
        f.write("\n")
    print(f"wrote {REGISTRY_PATH}: ratios={registry['allowed_ram_per_vcpu']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
