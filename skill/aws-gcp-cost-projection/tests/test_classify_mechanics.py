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
