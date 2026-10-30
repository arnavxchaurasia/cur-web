#!/usr/bin/env python3
r"""
tests/test_classify_invariants.py — Ordering-invariant and property tests for classify_mechanics.py.

Asserts the load-bearing ordering invariants directly against classify_mechanics.classify
and classify_mechanics.classify_with_trace:
1. Every (BoxUsage|HeavyUsage):t[234]g?\. + hourly row -> compute_burstable (NEVER compute_breakdown).
2. Every (BoxUsage|HeavyUsage):<family-with-g>\. Graviton row -> compute_arm.
3. Every RDS row with a non-hourly GB-Mo/storage unit -> block_storage (NEVER managed_db).
4. Every row with aws_amortized_cost < 0 -> negative_cost regardless of product.
5. Every classified group is a member of KNOWN_GROUPS = {g for g, _ in RULES} | {"misc"}.
6. classify_with_trace returns a label consistent with the group.

Runnable as:
  pytest tests/test_classify_invariants.py -q
  python tests/test_classify_invariants.py
"""
from __future__ import annotations
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import classify_mechanics as cm


def _row(**kw) -> dict:
    return {
        "aws_li_key": kw.get("aws_li_key", "k"),
        "product": kw.get("product", ""),
        "usage_type": kw.get("usage_type", ""),
        "operation": kw.get("operation", ""),
        "unit": kw.get("unit", ""),
        "line_item_type": kw.get("line_item_type", "Usage"),
        "pricing_model": kw.get("pricing_model", "OnDemand"),
        "aws_amortized_cost": kw.get("aws_amortized_cost", 100.0),
        "instance_type": kw.get("instance_type"),
        "instance_vcpus": kw.get("instance_vcpus"),
        "instance_ram_gb": kw.get("instance_ram_gb"),
        "instance_count": kw.get("instance_count"),
        "billing_format": kw.get("billing_format"),
        "product_code": kw.get("product_code"),
        "operating_system": kw.get("operating_system"),
        "database_engine": kw.get("database_engine"),
        "deployment_option": kw.get("deployment_option"),
        "volume_type": kw.get("volume_type"),
        "aws_region": kw.get("aws_region", "us-east-1"),
        "canonical": kw.get("canonical"),
    }


def test_invariant_burstable_never_breakdown():
    burstable_types = [
        "APS3-BoxUsage:t2.micro",
        "APS3-BoxUsage:t3.medium",
        "APS3-BoxUsage:t3a.large",
        "APS3-BoxUsage:t4g.xlarge",
        "HeavyUsage:t3.2xlarge",
    ]
    for ut in burstable_types:
        r = _row(
            product="Amazon Elastic Compute Cloud",
            product_code="AmazonEC2",
            usage_type=ut,
            operation="RunInstances",
            unit="Hrs",
        )
        group = cm.classify(r)
        assert group == "compute_burstable", f"{ut} expected compute_burstable, got {group}"


def test_invariant_graviton_never_x86_breakdown():
    graviton_types = [
        "APS3-BoxUsage:m6g.xlarge",
        "APS3-BoxUsage:c7g.2xlarge",
        "APS3-BoxUsage:r8g.4xlarge",
        "HeavyUsage:m7g.large",
    ]
    for ut in graviton_types:
        r = _row(
            product="Amazon Elastic Compute Cloud",
            product_code="AmazonEC2",
            usage_type=ut,
            operation="RunInstances",
            unit="Hrs",
        )
        group = cm.classify(r)
        assert group == "compute_arm", f"{ut} expected compute_arm, got {group}"


def test_invariant_rds_storage_never_managed_db():
    storage_rows = [
        ("APS3-StorageUsage", "GB-Mo"),
        ("APS3-GP2-Storage", "GB-Mo"),
        ("APS3-StorageIOPS", "IOPS-Mo"),
        ("APS3-BackupUsage", "GB-Mo"),
    ]
    for ut, unit in storage_rows:
        r = _row(
            product="Amazon Relational Database Service",
            product_code="AmazonRDS",
            usage_type=ut,
            operation="",
            unit=unit,
        )
        group = cm.classify(r)
        assert group == "block_storage", f"{ut} ({unit}) expected block_storage, got {group}"


def test_invariant_negative_cost_always_negative_cost():
    rows = [
        _row(product="Amazon Elastic Compute Cloud", usage_type="APS3-BoxUsage:m5.xlarge", aws_amortized_cost=-50.0),
        _row(product="Amazon Simple Storage Service", usage_type="APS3-TimedStorage-ByteHrs", aws_amortized_cost=-0.01),
        _row(product="AWS Support (Business)", usage_type="SupportCharges", aws_amortized_cost=-1000.0),
        _row(product="Amazon Relational Database Service", usage_type="APS3-InstanceUsage:db.r5.large", aws_amortized_cost=-10.0),
    ]
    for r in rows:
        group = cm.classify(r)
        assert group == "negative_cost", f"Row with negative cost {r['aws_amortized_cost']} classified as {group}"


def test_invariant_all_classified_groups_in_known_groups():
    known_groups = {group for group, _ in cm.RULES} | {"misc"}
    sample_rows = [
        _row(product="Amazon Elastic Compute Cloud", usage_type="APS3-BoxUsage:m5.xlarge", unit="Hrs"),
        _row(product="Amazon Elastic Compute Cloud", usage_type="APS3-NatGateway-Hours", unit="Hrs"),
        _row(product="Amazon Simple Storage Service", usage_type="APS3-Requests-Tier1", unit="Requests"),
        _row(product="Amazon CloudFront", usage_type="AP-DataTransfer-Out-Bytes", unit="GB"),
        _row(product="RandomUnknownUnlistedProduct", usage_type="UnlistedUsage", unit="Units"),
    ]
    for r in sample_rows:
        group = cm.classify(r)
        assert group in known_groups, f"Classified group {group!r} not in known_groups"


def test_invariant_classify_with_trace_consistency():
    sample_rows = [
        _row(product="Amazon Elastic Compute Cloud", usage_type="APS3-BoxUsage:m5.xlarge", unit="Hrs"),
        _row(product="Amazon Elastic Compute Cloud", usage_type="APS3-BoxUsage:t3.medium", unit="Hrs"),
        _row(product="Amazon Elastic Compute Cloud", usage_type="APS3-BoxUsage:m7g.large", unit="Hrs"),
        _row(product="Amazon Elastic Compute Cloud", usage_type="APS3-CPUCredits:t3", unit="Hrs"),
        _row(product="Amazon Elastic Compute Cloud", usage_type="APS3-NatGateway-Hours", unit="Hrs"),
        _row(product="RandomUnknownUnlistedProduct", usage_type="UnlistedUsage", unit="Units"),
    ]
    for r in sample_rows:
        group = cm.classify(r)
        group_trace, rule_label = cm.classify_with_trace(r)
        assert group == group_trace, f"classify ({group}) != classify_with_trace group ({group_trace})"
        # Post-cutover trace schemes: spec:<svc>.<ct>, spec_gap:<svc>,
        # prefilter:<name>, spec_error, legacy long-tail "<group>#<idx>", or "misc".
        if group == "misc":
            assert rule_label == "misc" or rule_label.startswith(("spec_gap:", "spec_error")), \
                f"misc group should have rule_label 'misc'/'spec_gap:*'/'spec_error', got {rule_label}"
        else:
            assert (rule_label.startswith(("spec:", "prefilter:"))
                    or rule_label.startswith(f"{group}#")), \
                f"rule_label {rule_label} inconsistent with group {group}"


def main():
    tests = [
        ("Burstable instances never breakdown", test_invariant_burstable_never_breakdown),
        ("Graviton instances never x86 breakdown", test_invariant_graviton_never_x86_breakdown),
        ("RDS storage rows never managed_db", test_invariant_rds_storage_never_managed_db),
        ("Negative cost rows always negative_cost", test_invariant_negative_cost_always_negative_cost),
        ("All classified groups in known groups", test_invariant_all_classified_groups_in_known_groups),
        ("classify_with_trace consistent with classify", test_invariant_classify_with_trace_consistency),
    ]

    print(f"{'Test Case':<60} {'Status'}")
    print("-" * 70)
    all_passed = True
    for name, fn in tests:
        try:
            fn()
            print(f"{name:<60} PASS")
        except Exception as e:
            print(f"{name:<60} FAIL: {e}")
            all_passed = False

    print("-" * 70)
    if all_passed:
        print("All tests passed.")
        return 0
    else:
        print("Some tests failed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
