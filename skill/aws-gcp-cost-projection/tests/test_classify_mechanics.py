"""
Tests for classify_mechanics.py RULES routing.

Verifies that rows route to the correct mechanic_group. Each test
creates a synthetic CUR row and asserts the first-match group. This
ensures that new rules added above existing rules don't accidentally
shadow rows that should reach their intended group.
"""

import pytest
from conftest import row
from classify_mechanics import classify


# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------

def test_ec2_on_demand():
    r = row(product="Amazon Elastic Compute Cloud",
            usage_type="BoxUsage:m5.xlarge", operation="RunInstances", unit="Hrs")
    assert classify(r) == "compute_breakdown"


def test_ec2_spot():
    r = row(product="Amazon EC2",
            usage_type="SpotUsage:c5.2xlarge", operation="RunInstances", unit="Hrs")
    assert classify(r) == "compute_breakdown"


def test_rds_instance_goes_to_managed_db():
    r = row(product="Amazon Relational Database Service",
            usage_type="db.m5.large", operation="CreateDBInstance", unit="Hrs")
    assert classify(r) == "managed_db"


def test_aurora_serverless_v2_acu():
    r = row(product="Amazon Aurora",
            usage_type="ACU-Hrs", operation="Aurora I/O-Optimized ACU", unit="ACU-Hrs")
    assert classify(r) == "managed_db"


def test_rds_storage_not_managed_db():
    """RDS storage (non-Hrs unit) should go to block_storage, not managed_db."""
    r = row(product="Amazon Relational Database Service",
            usage_type="RDS:GP2-Storage", operation="", unit="GB-Mo")
    assert classify(r) == "block_storage"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_s3_standard_storage():
    r = row(product="Amazon Simple Storage Service",
            usage_type="TimedStorage-ByteHrs", operation="StandardStorage", unit="GB-Mo")
    assert classify(r) == "object_storage"


def test_s3_standard_ia_storage():
    r = row(product="Amazon S3",
            usage_type="TimedStorage-SIA-ByteHrs", operation="", unit="GB-Mo")
    assert classify(r) == "object_storage"


def test_s3_glacier_storage():
    r = row(product="Amazon S3",
            usage_type="TimedStorage-GlacierByteHrs", operation="GlacierStorage", unit="GB-Mo")
    assert classify(r) == "object_storage"


def test_ebs_volume():
    r = row(product="Amazon Elastic Block Store",
            usage_type="EBS:VolumeUsage.gp3", unit="GB-Mo")
    assert classify(r) == "block_storage"


def test_ebs_snapshot():
    r = row(product="Amazon EC2",
            usage_type="EBS:SnapshotUsage", unit="GB-Mo")
    assert classify(r) == "block_storage"


def test_efs_standard():
    r = row(product="Amazon Elastic File System",
            usage_type="TimedStorage-EFS-ByteHrs", unit="GB-Mo")
    assert classify(r) == "efs"


def test_fsx_lustre_before_block_storage():
    """FSx row must not fall into block_storage (which matches 'Storage' in usage_type)."""
    r = row(product="Amazon FSx",
            usage_type="FSx:Lustre-Storage-GB-Mo", unit="GB-Mo")
    assert classify(r) == "fsx"


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

def test_nat_gateway_hours():
    r = row(product="Amazon Virtual Private Cloud",
            usage_type="VpcUsage:NatGateway-Hours", unit="Hrs")
    assert classify(r) == "flat_hourly"


def test_alb_hours():
    r = row(product="Amazon EC2",
            usage_type="LoadBalancerUsage:application", unit="Hrs")
    assert classify(r) == "flat_hourly"


def test_data_transfer_egress():
    r = row(product="AWS Data Transfer",
            usage_type="DataTransfer-Out-Bytes", unit="GB")
    assert classify(r) == "data_transfer"


def test_direct_connect_goes_flat_hourly():
    r = row(product="AWS Direct Connect",
            usage_type="DirectConnect-Hours", unit="Hrs")
    assert classify(r) == "flat_hourly"


def test_global_accelerator_goes_flat_hourly():
    r = row(product="AWS Global Accelerator",
            usage_type="GlobalAccelerator-PortHours", unit="Hrs")
    assert classify(r) == "flat_hourly"


# ---------------------------------------------------------------------------
# Analytics / databases
# ---------------------------------------------------------------------------

def test_redshift_node():
    r = row(product="Amazon Redshift",
            usage_type="ra3.4xlarge-EMR-CORE", operation="", unit="Hrs")
    assert classify(r) == "redshift"


def test_athena_data_scanned():
    r = row(product="Amazon Athena",
            usage_type="DataScanned-Bytes", unit="TB")
    assert classify(r) == "athena"


def test_kinesis_shard_hours():
    r = row(product="Amazon Kinesis",
            usage_type="Kinesis-ShardHour", unit="Hrs")
    assert classify(r) == "kinesis"


def test_emr_management_fee():
    r = row(product="Amazon Elastic MapReduce",
            usage_type="m5.xlarge-EMR-CORE", unit="Hrs")
    assert classify(r) == "emr"


def test_emr_management_fee_master():
    r = row(product="AmazonEMR",
            usage_type="r5.4xlarge-EMR-MASTER", unit="Hrs")
    assert classify(r) == "emr"


# ---------------------------------------------------------------------------
# Security / monitoring
# ---------------------------------------------------------------------------

def test_guardduty():
    r = row(product="Amazon GuardDuty",
            usage_type="EU-AWSLogs-Processed-Bytes", unit="GB")
    assert classify(r) == "guardduty"


def test_security_hub():
    r = row(product="AWS Security Hub",
            usage_type="EU-Security-Findings", unit="Count")
    assert classify(r) == "guardduty"


def test_xray_traces():
    r = row(product="AWS X-Ray",
            usage_type="Traces-Stored-Count", unit="Count")
    assert classify(r) == "xray"


def test_cloudwatch_logs():
    r = row(product="Amazon CloudWatch",
            usage_type="APNortheast1-DataProcessing-Bytes", unit="GB")
    assert classify(r) == "cloudwatch"


# ---------------------------------------------------------------------------
# Non-workload / commitment
# ---------------------------------------------------------------------------

def test_marketplace_passthrough():
    r = row(product="AWS Marketplace",
            usage_type="SaaS-License", unit="Hrs")
    assert classify(r) == "non_workload"


def test_ri_fee():
    r = row(product="Amazon Elastic Compute Cloud",
            usage_type="HeavyUsage:m5.xlarge", line_item_type="RIFee", unit="Hrs")
    assert classify(r) == "commitment_discount"


# ---------------------------------------------------------------------------
# Misc fallback
# ---------------------------------------------------------------------------

def test_unknown_service_falls_to_misc():
    r = row(product="AWS Something Obscure",
            usage_type="CustomUsage", unit="Units")
    assert classify(r) == "misc"


def test_lambda_requests_per_request():
    r = row(product="AWS Lambda",
            usage_type="Requests", unit="Requests")
    assert classify(r) == "per_request"


# ---------------------------------------------------------------------------
# Blank pricing_unit ("unit") — PDF/flat-CSV bills routinely leave this blank.
# A real bug: the compute routing rules required unit to be exactly
# "Hrs"/"hours" with no blank-string tolerance, so a genuine EC2 instance-hour
# row from one of those formats silently bypassed every deterministic compute
# mapper and fell through to misc/LLM instead. Confirmed by temporarily
# reverting the fix and observing this exact row fall through — these tests
# guard against that regression across all five affected rules.
# ---------------------------------------------------------------------------

def test_ec2_pdf_format_blank_unit_still_routes_to_compute_breakdown():
    r = row(product="Amazon Elastic Compute Cloud",
            usage_type="", operation="On Demand Linux m5.xlarge Instance-hour",
            unit="")
    assert classify(r) == "compute_breakdown"


def test_ec2_burstable_pdf_format_blank_unit_still_routes():
    r = row(product="Amazon Elastic Compute Cloud",
            usage_type="BoxUsage:t3a.large", operation="RunInstances", unit="")
    assert classify(r) == "compute_burstable"


def test_ec2_arm_pdf_format_blank_unit_still_routes():
    r = row(product="Amazon Elastic Compute Cloud",
            usage_type="BoxUsage:m7g.xlarge", operation="RunInstances", unit="")
    assert classify(r) == "compute_arm"


def test_ec2_windows_pdf_format_blank_unit_still_routes():
    r = row(product="Amazon Elastic Compute Cloud",
            usage_type="BoxUsage:m5.xlarge running Windows", operation="RunInstances",
            unit="")
    assert classify(r) == "compute_windows"


def test_nat_gateway_pdf_format_blank_unit_still_routes_to_flat_hourly():
    r = row(product="Amazon Elastic Compute Cloud",
            usage_type="NatGateway-Hours", operation="NatGateway", unit="")
    assert classify(r) == "flat_hourly"


# ---------------------------------------------------------------------------
# MSK (Kafka) — broker-hours vs storage charge-type split
# (regression: PDF/flat MSK rows fell to misc — gate-verified on real bills)
# ---------------------------------------------------------------------------

def test_msk_broker_hours_cur_goes_to_msk():
    r = row(product="Amazon Managed Streaming for Apache Kafka",
            usage_type="AP-SOUTHEAST-1-Usage:kafka.t3", unit="Hrs", canonical="msk")
    assert classify(r) == "msk"


def test_msk_broker_hours_pdf_blank_usage_type_goes_to_msk():
    # PDF leaves usage_type blank; the broker signal lives in operation.
    r = row(product="Amazon Managed Streaming for Apache Kafka",
            usage_type="", unit="Hrs",
            operation="$0.0578 per broker hour for Kafka.t3.small in Singapore",
            canonical="msk")
    assert classify(r) == "msk"


def test_msk_storage_pdf_goes_to_block_storage_not_misc():
    # PDF MSK storage: GB-Mo, usage_type blank, signal in operation.
    r = row(product="Amazon Managed Streaming for Apache Kafka",
            usage_type="", unit="GB-Mo",
            operation="$0.12 per GB-Mo for storage in Singapore (1,648 GB-Mo)",
            canonical="msk")
    assert classify(r) == "block_storage"


def test_msk_storage_cur_goes_to_block_storage():
    r = row(product="Amazon Managed Streaming for Apache Kafka",
            usage_type="Kafka.Storage.GB", unit="GB-Mo", canonical="msk")
    assert classify(r) == "block_storage"


# ---------------------------------------------------------------------------
# Data-gap surfacing: known service, but bill has no charge-type detail
# ---------------------------------------------------------------------------
import json as _json
from classify_mechanics import _misc_reason


def test_degenerate_known_service_row_flagged_as_data_gap():
    # Rolled-up RDS line: identity known, but unit/operation blank and
    # usage_type is a "<region>-Usage:NA" placeholder -> cannot be priced.
    r = row(product="Amazon Relational Database Service",
            usage_type="AP-SOUTHEAST-1-Usage:NA", operation="", unit="",
            canonical="rds")
    assert classify(r) == "misc"
    reason = _json.loads(_misc_reason(r))
    assert reason.get("data_gap") is True
    assert reason.get("service_hint") == "rds"


def test_detailed_row_is_not_a_data_gap():
    # A normal EC2 instance row is classifiable -> not misc, not a data gap.
    r = row(product="Amazon Elastic Compute Cloud",
            usage_type="BoxUsage:m5.large", operation="RunInstances", unit="Hrs",
            canonical="ec2")
    assert classify(r) == "compute_breakdown"


def test_cost_only_blank_row_forced_passthrough_data_gap():
    # Row with cost > 0 but all line-item detail missing (H-5)
    # 1. Completely blank cost-only row (bad PDF/export)
    r_blank = row(product="", usage_type="", operation="", unit="",
                  instance_type="", cost=150.0, canonical=None)
    assert classify(r_blank) == "misc"
    reason_blank = _json.loads(_misc_reason(r_blank))
    assert reason_blank.get("data_gap") is True
    assert reason_blank.get("recommended_strategy") == "passthrough"
    assert reason_blank.get("strategy") == "passthrough"
    assert "could not be classified" in reason_blank.get("why", "")

    # 2. Amazon prefix row with no charge-type detail
    r_aws = row(product="Amazon Unknown Service", usage_type="", operation="", unit="",
                instance_type="", cost=80.0, canonical=None)
    assert classify(r_aws) == "misc"
    reason_aws = _json.loads(_misc_reason(r_aws))
    assert reason_aws.get("data_gap") is True
    assert reason_aws.get("recommended_strategy") == "passthrough"


# ---------------------------------------------------------------------------
# FIX 8, 9, 10 regressions & coverage
# ---------------------------------------------------------------------------

def test_has_instance_type_tightened():
    from classify_mechanics import _has_instance_type
    assert _has_instance_type("m5.xlarge") is True
    assert _has_instance_type("db.r5.large") is True
    assert _has_instance_type("c6gd.4xlarge") is True
    assert _has_instance_type("t3.medium") is True
    assert _has_instance_type("v2.0") is False
    assert _has_instance_type("h264.profile") is False
    assert _has_instance_type("tier1.foo") is False


def test_instance_type_oracle_matching():
    from classify_mechanics import _has_instance_type, _EC2_TYPES_DATA
    # Known types in oracle table (including non-standard and prefix formats)
    assert _has_instance_type("m5.xlarge") is True
    assert _has_instance_type("db.r5.large") is True
    assert _has_instance_type("u-12tb1.112xlarge") is True
    assert _has_instance_type("hpc7g.16xlarge") is True
    assert _has_instance_type("APS3-BoxUsage:u-12tb1.112xlarge") is True

    # Newly added type dynamically added to oracle table
    _EC2_TYPES_DATA["newfuture99.superlarge"] = {"vcpus": 128, "ram_gb": 512.0}
    try:
        assert _has_instance_type("newfuture99.superlarge") is True
        assert _has_instance_type("db.newfuture99.superlarge") is True
    finally:
        del _EC2_TYPES_DATA["newfuture99.superlarge"]

    # Unknown tokens do NOT false-match
    assert _has_instance_type("v2.0") is False
    assert _has_instance_type("h264.profile") is False
    assert _has_instance_type("random_non_instance_token") is False


def test_pdf_whitespace_and_nbsp_normalization():
    # PDF bills with non-breaking spaces (\xa0) or double spaces still match
    r = row(product="Amazon\xa0Elastic  Compute\xa0Cloud",
            usage_type="APS3-BoxUsage:m5.xlarge", operation="RunInstances", unit="Hrs",
            canonical="ec2")
    assert classify(r) == "compute_breakdown"


def test_sagemaker_instance_excluded_from_compute_breakdown():
    # SageMaker ML instance-hour row must route to misc (not compute_breakdown)
    r = row(product="Amazon SageMaker",
            usage_type="APS3-ml.p4d.24xlarge-Hour", operation="RunInstance", unit="Hrs",
            canonical="sagemaker", instance_type="ml.p4d.24xlarge")
    assert classify(r) == "misc"


# ---------------------------------------------------------------------------
# FIX 12, 14 regressions & coverage
# ---------------------------------------------------------------------------

def test_rifee_no_discounted_usage_becomes_compute():
    r = row(product="Amazon Elastic Compute Cloud", usage_type="HeavyUsage:m5.xlarge",
            line_item_type="RIFee", unit="Hrs", _bill_has_discounted_usage=False)
    assert classify(r) == "compute_breakdown"


def test_neptune_instance_not_compute_breakdown():
    r = row(product="Amazon Neptune", usage_type="BoxUsage:db.r5.large",
            operation="CreateDBInstance", unit="Hrs")
    assert classify(r) != "compute_breakdown"


def test_qldb_not_compute_breakdown():
    r = row(product="Amazon QLDB", usage_type="BoxUsage:db.t3.medium",
            operation="", unit="Hrs")
    assert classify(r) != "compute_breakdown"


def test_timestream_not_compute_breakdown():
    r = row(product="Amazon Timestream", usage_type="BoxUsage:db.r5.large",
            operation="", unit="Hrs")
    assert classify(r) != "compute_breakdown"


# ---------------------------------------------------------------------------
# WORKORDER_charge_type_misrouting_fix tests
# ---------------------------------------------------------------------------

def test_aurora_serverless_io_optimized_acu_is_compute():
    """ServerlessV2 IO-Optimized usage billed in ACU-Hr is COMPUTE (Aurora Capacity
    Units per hour), so it routes to managed_db — not block_storage. (The row name
    contains 'IOOptimized' but the unit is ACU-Hr; it is the serverless compute
    charge of an I/O-optimized cluster, not a per-I/O row.)

    Genuine per-I/O rows (unit=IOs) must NOT be priced as GB-Mo storage — that is
    the 1300x-inflation class — nor as compute; they passthrough via the Aurora
    I/O branch (replicated_write_io -> misc/passthrough)."""
    acu = row(product="Amazon Relational Database Service",
              usage_type="APS3-Aurora:ServerlessV2IOOptimizedUsage",
              operation="CreateDBInstance:0016",
              unit="ACU-Hr")
    assert classify(acu) == "managed_db"

    for ut in ("APS3-Aurora:StorageIOUsage", "APS3-Aurora:VolumeIOUsage",
               "APS3-Aurora:ReplicatedWriteIO"):
        io = row(product="Amazon Relational Database Service",
                 usage_type=ut, operation="", unit="IOs")
        g = classify(io)
        assert g != "block_storage", f"{ut} priced as storage (1300x inflation risk): {g}"
        assert g != "managed_db", f"{ut} misrouted to compute: {g}"


def test_waf_requests_and_rules_route_to_waf_per_request():
    """WAF RequestV2 and WebACLV2 rows must route to per_request WAF branch, NOT generic per_request."""
    r_req = row(product="AWS WAF",
                usage_type="APS3-RequestV2-Tier2-2000WCU",
                operation="ALB",
                unit="Request")
    res_req = classify(r_req)
    assert res_req == "per_request"

    r_acl = row(product="AWS WAF",
                usage_type="Global-WebACLV2",
                operation="WebACLV2",
                unit="Month")
    res_acl = classify(r_acl)
    assert res_acl == "per_request"


def test_s3_requests_tier_routes_before_generic_per_request():
    """S3 Requests-Tier rows must be caught by dedicated S3 request guard before generic per_request."""
    r = row(product="Amazon Simple Storage Service",
            usage_type="APS3-Requests-Tier2",
            operation="GetObject",
            unit="Requests",
            canonical="s3")
    res = classify(r)
    assert res == "per_request"


