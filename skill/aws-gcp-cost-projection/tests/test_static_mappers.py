"""
Tests for apply_static_mappings.py static mapper functions.

Each test calls a mapper directly with synthetic input rows and asserts
the output fields — gcp_service, strategy, unit_multiplier, and gcp_sku_name.
No DuckDB or live GCP API calls are made (resolve_sku may return None for
unknown SKUs, which is fine — we test for strategy correctness, not SKU IDs).

Golden values tested:
  - S3 Glacier Deep Archive → Archive Storage (NOT Coldline — past 120x inflation bug)
  - S3 per-request → passthrough (NOT mapped — past 50x inflation bug)
  - Redshift ra3.4xlarge → 1500 BQ slots
  - Redshift serverless RPU → 128 slots/RPU
  - GuardDuty → Security Command Center passthrough
  - EFS Standard-IA → Filestore Basic HDD
  - FSx Lustre → Filestore High Scale SSD
  - EMR m5.xlarge → 4 vCPU Dataproc Premium
  - Athena data-scanned → BigQuery Analysis map
  - Kinesis shard-hours → Pub/Sub passthrough
  - X-Ray → Cloud Trace
  - S3 monitoring-fee → passthrough (no-equivalent)
"""

import pytest
from unittest.mock import patch
from conftest import row

# Patch resolve_sku to return a synthetic SKU without hitting the catalog.
# resolve_sku returns SKUMeta (a str subclass carrying .unit/.resource_group) —
# the mock must match or mappers that read sku_id.unit crash with AttributeError.
from apply_static_mappings import SKUMeta
MOCK_SKU = SKUMeta("MOCK-SKU-1234", unit="h", resource_group="Mock")


def _with_sku(mapper, rows):
    """Call mapper with resolve_sku patched to always return MOCK_SKU.

    Some mappers (map_object_storage) return (mapped, llm_fallback_rows);
    normalize to the mapped list so assertions work uniformly."""
    with patch("apply_static_mappings.resolve_sku", return_value=MOCK_SKU):
        result = mapper(rows)
    if isinstance(result, tuple):
        return result[0]
    return result


# Import mappers after path is configured by conftest
from apply_static_mappings import (
    map_object_storage, map_per_request, map_block_storage,
    map_data_transfer, map_non_workload, map_cloudwatch,
    map_guardduty, map_redshift, map_athena, map_kinesis,
    map_efs, map_fsx, map_xray, map_emr, map_compute_windows, map_compute_arm,
    map_compute_burstable, map_flat_hourly,
    _emr_vcpus,
)


# ---------------------------------------------------------------------------
# Object storage (S3 → GCS)
# ---------------------------------------------------------------------------

def test_s3_standard_maps_to_standard_storage():
    rows = [row(product="Amazon S3", usage_type="TimedStorage-ByteHrs",
                operation="StandardStorage", unit="GB-Mo")]
    out = _with_sku(map_object_storage, rows)
    assert len(out) == 1
    assert out[0]["gcp_service"] == "Cloud Storage"
    assert out[0]["gcp_sku_name"] == "Standard Storage"
    assert out[0]["strategy"] == "map"


def test_s3_glacier_deep_archive_maps_to_archive_not_coldline():
    """Regression: Glacier Deep Archive must NOT map to Coldline (120x inflation bug)."""
    rows = [row(product="Amazon S3", usage_type="TimedStorage-GlacierDeepArchive-ByteHrs",
                operation="GlacierDeepArchiveStorage", unit="GB-Mo")]
    out = _with_sku(map_object_storage, rows)
    assert out[0]["gcp_sku_name"] == "Archive Storage"


def test_s3_glacier_flexible_maps_to_coldline():
    rows = [row(product="Amazon S3", usage_type="TimedStorage-GlacierFlexible-ByteHrs",
                operation="GlacierFlexible", unit="GB-Mo")]
    out = _with_sku(map_object_storage, rows)
    assert out[0]["gcp_sku_name"] == "Coldline Storage"


def test_s3_standard_ia_maps_to_nearline():
    rows = [row(product="Amazon S3", usage_type="TimedStorage-SIA-ByteHrs",
                operation="StandardIA", unit="GB-Mo")]
    out = _with_sku(map_object_storage, rows)
    assert out[0]["gcp_sku_name"] == "Nearline Storage"


def test_s3_monitoring_fee_ignored():
    """S3 IT per-object monitoring fee: GCS Autoclass has no charge — must be ignore ($0)."""
    rows = [row(product="Amazon S3",
                usage_type="Monitoring-Automation-INT",
                operation="per 1,000 objects monitored", unit="Count")]
    out = _with_sku(map_object_storage, rows)
    assert out[0]["strategy"] == "ignore"


# ---------------------------------------------------------------------------
# Per-request (S3 request → GCS Class A/B Operations)
# ---------------------------------------------------------------------------

def test_s3_tier1_maps_to_class_a():
    """S3 Tier 1 (PUT/LIST) → GCS Class A Operations at parity pricing."""
    rows = [row(product="Amazon Simple Storage Service",
                usage_type="Requests-Tier1", unit="Requests")]
    out = _with_sku(map_per_request, rows)
    assert out[0]["gcp_service"] == "Cloud Storage"
    assert out[0]["strategy"] in ("map", "passthrough")  # map when SKU resolves
    assert out[0]["unit_multiplier"] == 1.0


def test_s3_tier2_maps_to_class_b():
    """S3 Tier 2 (GET) → GCS Class B Operations at parity pricing."""
    rows = [row(product="Amazon Simple Storage Service",
                usage_type="Requests-Tier2", unit="Requests")]
    out = _with_sku(map_per_request, rows)
    assert out[0]["gcp_service"] == "Cloud Storage"
    assert "Class B" in (out[0].get("gcp_sku_name") or "") or out[0]["strategy"] == "passthrough"


def test_s3_lifecycle_requests_passthrough():
    """S3 lifecycle transition requests have no GCS equivalent — passthrough."""
    rows = [row(product="Amazon Simple Storage Service",
                usage_type="Requests-Tier1", operation="Lifecycle Transition",
                unit="Requests")]
    out = _with_sku(map_per_request, rows)
    assert out[0]["strategy"] == "passthrough"


def test_lambda_maps_to_cloud_run():
    # Real GCP service is "Cloud Run Functions", not sibling service "Cloud
    # Run" — confirmed via the real catalog that "Cloud Run" has no
    # Lambda-relevant SKUs at all, so the old target service name always
    # resolved to nothing.
    rows = [row(product="AWS Lambda", usage_type="Requests", unit="Requests")]
    out = _with_sku(map_per_request, rows)
    assert out[0]["gcp_service"] == "Cloud Run Functions"
    assert out[0]["strategy"] == "map"


# ---------------------------------------------------------------------------
# Block storage
# ---------------------------------------------------------------------------

def test_ebs_gp3_maps_to_hyperdisk_balanced():
    # gp3 gets a separate provisioned-IOPS/throughput fee row (priced against
    # Hyperdisk Balanced IOPS/Throughput SKUs elsewhere in this function) —
    # capacity must stay on Hyperdisk Balanced Capacity, not classic Balanced
    # PD (a different, non-interoperable product family with no IOPS SKU of
    # its own — pairing classic-PD capacity with Hyperdisk-only performance
    # add-ons on the same disk isn't purchasable on real GCP).
    rows = [row(product="Amazon Elastic Block Store",
                usage_type="EBS:VolumeUsage.gp3", volume_type="gp3", unit="GB-Mo")]
    out = _with_sku(map_block_storage, rows)
    assert out[0]["gcp_service"] == "Compute Engine"
    assert out[0]["gcp_sku_name"] == "Hyperdisk Balanced Capacity"


def test_ebs_gp2_maps_to_classic_balanced_pd():
    # gp2 never gets a separate provisioned-IOPS/throughput fee (bundled into
    # capacity, like classic Balanced PD) — classic Balanced PD Capacity is
    # a correct, standalone match; no product-family mixing concern here.
    rows = [row(product="Amazon Elastic Block Store",
                usage_type="EBS:VolumeUsage.gp2", volume_type="gp2", unit="GB-Mo")]
    out = _with_sku(map_block_storage, rows)
    assert out[0]["gcp_sku_name"] == "Balanced PD Capacity"


def test_ebs_io1_standard_maps_to_hyperdisk_balanced():
    # Standard io1/io2 (not "Block Express") caps at 64,000 IOPS / 1,000 MB/s
    # per volume — safely under Hyperdisk Balanced's per-volume ceiling
    # (160,000 IOPS / 2,400 MiB/s), so Hyperdisk Balanced Capacity (cheaper,
    # and the correct product family for a volume that can also carry a
    # provisioned-IOPS fee) is the target, not Extreme PD or classic
    # Balanced PD.
    rows = [row(product="Amazon EC2",
                usage_type="EBS:VolumeUsage.io1", volume_type="io1", unit="GB-Mo")]
    out = _with_sku(map_block_storage, rows)
    assert out[0]["gcp_sku_name"] == "Hyperdisk Balanced Capacity"


def test_ebs_io2_block_express_maps_to_extreme_pd():
    # io2 Block Express can reach 256,000 IOPS / 4,000 MB/s — genuinely
    # exceeds Hyperdisk Balanced's ceiling, so Extreme is the correct (only
    # capable) target here, not a downgrade.
    #
    # GCP publishes this same Extreme tier under two SKU generations — legacy
    # "Extreme PD Capacity" and current-generation "Hyperdisk Extreme
    # Capacity" — and a live catalog sweep found the cheaper one varies by
    # region (some regions, e.g. us-central1 used by the row() default, only
    # have the Hyperdisk-generation SKU at all). The mapper dynamically picks
    # whichever real SKU is cheapest/available in-region rather than hardcoding
    # one name, so this only asserts the performance TIER (Extreme), not which
    # generation's name won for this region.
    rows = [row(product="Amazon EC2",
                usage_type="EBS:VolumeUsage.io2", operation="io2 Block Express volume",
                volume_type="io2", unit="GB-Mo")]
    out = _with_sku(map_block_storage, rows)
    assert "Extreme" in out[0]["gcp_sku_name"]


def test_ebs_snapshot_maps_to_snapshot_sku():
    rows = [row(product="Amazon EC2",
                usage_type="EBS:SnapshotUsage", unit="GB-Mo")]
    out = _with_sku(map_block_storage, rows)
    assert "Snapshot" in out[0]["gcp_sku_name"]


def test_rds_io_request_ignored():
    """Aurora/RDS per-I/O rows must be ignored (otherwise unit mismatch inflates 1000x)."""
    rows = [row(product="Amazon Aurora",
                usage_type="RDS:Aurora:IO-Request",
                operation="I/O request", unit="IOs")]
    out = _with_sku(map_block_storage, rows)
    assert out[0]["strategy"] == "ignore"


def test_gp3_iops_mapped_to_hyperdisk():
    """gp3 Provisioned IOPS maps to Hyperdisk Balanced IOPS (not ignored)."""
    rows = [row(product="Amazon EC2",
                usage_type="EBS:VolumeUsage.gp3-IOPS-mo",
                operation="Provisioned IOPS", unit="IOPS-Mo")]
    out = _with_sku(map_block_storage, rows)
    assert out[0]["strategy"] in ("map", "passthrough")  # map when SKU resolves, passthrough when not in test catalog
    assert out[0]["component"] == "iops"
    assert "Hyperdisk Balanced IOPS" in (out[0].get("gcp_sku_name") or "")


# ---------------------------------------------------------------------------
# Windows EC2
# ---------------------------------------------------------------------------

def test_windows_ec2_emits_three_components():
    """Windows EC2 rows must emit core + ram + license components."""
    rows = [row(product="Amazon Elastic Compute Cloud running Windows",
                usage_type="Amazon Elastic Compute Cloud running Windows",
                operation="On Demand Windows t2.xlarge Instance Hour",
                unit="Hrs", gcp_region="us-east4")]
    out = _with_sku(map_compute_windows, rows)
    components = {r["component"] for r in out}
    assert "core" in components
    assert "ram" in components
    assert "license" in components
    license_row = next(r for r in out if r["component"] == "license")
    # Standard Edition ("...Standard Edition on VM") is billed flat per VM-hour,
    # not per-vCPU like Datacenter Edition ("...(CPU cost)") — AWS's generic
    # License-Included Windows product is Standard, not Datacenter.
    assert license_row["unit_multiplier"] == 1.0
    assert license_row["gcp_sku_id"] == "1991-54F9-2129"


def test_windows_ec2_unknown_type_passthrough():
    """Unknown Windows instance type falls through to passthrough."""
    rows = [row(product="Amazon Elastic Compute Cloud running Windows",
                usage_type="Amazon Elastic Compute Cloud running Windows",
                operation="On Demand Windows x99.custom Instance Hour",
                unit="Hrs", gcp_region="us-east4")]
    out = _with_sku(map_compute_windows, rows)
    assert len(out) == 1
    assert out[0]["strategy"] == "passthrough"


# ---------------------------------------------------------------------------
# ARM (Graviton) EC2
# ---------------------------------------------------------------------------

def test_arm_ec2_emits_core_and_ram():
    """Graviton EC2 rows emit core + ram components (C4A or N2D fallback)."""
    rows = [row(product="Amazon Elastic Compute Cloud",
                usage_type="APS5-BoxUsage:m8g.2xlarge",
                operation="RunInstances", unit="Hrs", gcp_region="asia-south2")]
    out = _with_sku(map_compute_arm, rows)
    components = {r["component"] for r in out}
    assert "core" in components
    assert "ram" in components
    # m8g.2xlarge: 8 vCPU, 4 GiB/vCPU = 32 GiB
    core_row = next(r for r in out if r["component"] == "core")
    ram_row  = next(r for r in out if r["component"] == "ram")
    assert core_row["unit_multiplier"] == 8.0
    assert ram_row["unit_multiplier"]  == 32.0


def test_arm_ec2_t4g_burstable_ram():
    """t4g uses irregular burstable RAM schedule (not ratio × vCPU)."""
    rows = [row(product="Amazon Elastic Compute Cloud",
                usage_type="APS3-BoxUsage:t4g.large",
                operation="RunInstances", unit="Hrs", gcp_region="asia-south1")]
    out = _with_sku(map_compute_arm, rows)
    ram_row = next(r for r in out if r["component"] == "ram")
    assert ram_row["unit_multiplier"] == 8.0   # t4g.large = 8 GiB


def test_arm_ec2_unparseable_passthrough():
    """Unknown Graviton usage_type falls through to passthrough."""
    rows = [row(product="Amazon Elastic Compute Cloud",
                usage_type="APS5-BoxUsage:unkfamilyg.weirdsize",
                operation="RunInstances", unit="Hrs", gcp_region="asia-south2")]
    out = _with_sku(map_compute_arm, rows)
    assert len(out) == 1
    assert out[0]["strategy"] == "passthrough"


# ---------------------------------------------------------------------------
# GuardDuty / Security Hub
# ---------------------------------------------------------------------------

def test_guardduty_passthrough_to_scc():
    rows = [row(product="Amazon GuardDuty",
                usage_type="EU-AWSLogs-Processed-Bytes", unit="GB")]
    out = map_guardduty(rows)
    assert out[0]["gcp_service"] == "Security Command Center"
    assert out[0]["strategy"] == "passthrough"


def test_security_hub_passthrough_to_scc():
    rows = [row(product="AWS Security Hub",
                usage_type="Security-Findings", unit="Count")]
    out = map_guardduty(rows)
    assert out[0]["gcp_service"] == "Security Command Center"
    assert out[0]["strategy"] == "passthrough"


# ---------------------------------------------------------------------------
# Redshift → BigQuery
# ---------------------------------------------------------------------------

def test_redshift_ra3_4xlarge_node_hour_is_honest_passthrough():
    # No official AWS or Google source publishes a Redshift-node-to-BigQuery-
    # slot equivalence (checked both vendors' docs directly — see
    # apply_static_mappings.py's _REDSHIFT_SLOT_MAP comment). The old
    # unit_multiplier=1500 "map" strategy produced a confirmed +1761%
    # overprojection on a real job's ra3.4xlarge node-hour rows and, layered
    # with a separate seconds/hours bug on Concurrency Scaling rows, a
    # $75.5M/mo phantom charge. This now passthroughs, same treatment as the
    # Reserved-Instance case in the same function.
    rows = [row(product="Amazon Redshift",
                usage_type="ra3.4xlarge-NodeUsage", unit="Hrs")]
    out = _with_sku(map_redshift, rows)
    assert out[0]["gcp_service"] == "BigQuery Reservation API"
    assert out[0]["strategy"] == "passthrough"
    assert out[0]["unit_multiplier"] == 1.0


def test_redshift_dc2_large_node_hour_is_honest_passthrough():
    rows = [row(product="Amazon Redshift",
                usage_type="dc2.large-NodeUsage", unit="Hrs")]
    out = _with_sku(map_redshift, rows)
    assert out[0]["strategy"] == "passthrough"
    assert out[0]["unit_multiplier"] == 1.0


def test_redshift_serverless_rpu_is_honest_passthrough():
    # "1 RPU ~= 128 BQ slots" had the same unsourced-ratio problem as the
    # node-hour table (AWS's own RPU spec is ~2 vCPU/16GB with no published
    # slot equivalence, and Google's BigQuery slots doc doesn't publish a
    # vCPU/slot ratio either) — passthrough for the same reason.
    rows = [row(product="Amazon Redshift Serverless",
                usage_type="ServerlessRPUHours", unit="Hrs")]
    out = _with_sku(map_redshift, rows)
    assert out[0]["strategy"] == "passthrough"
    assert out[0]["unit_multiplier"] == 1.0


def test_redshift_concurrency_scaling_is_passthrough_not_node_hour():
    # CONFIRMED REAL BUG: "APS3-CS:ra3.4xlarge" (Concurrency Scaling, billed
    # in raw SECONDS) used to match the node-hour family-substring loop
    # (matched "ra3.4xlarge") and get treated as if total_usage were HOURS —
    # a ~3600x unit blowup stacked on the mapping itself. Must route to
    # passthrough before the node-hour loop, never through it.
    rows = [row(product="Amazon Redshift",
                usage_type="APS3-CS:ra3.4xlarge", unit="seconds")]
    out = _with_sku(map_redshift, rows)
    assert out[0]["strategy"] == "passthrough"
    assert out[0]["unit_multiplier"] == 1.0


def test_redshift_backup_passthrough():
    rows = [row(product="Amazon Redshift",
                usage_type="SnapshotUsage", unit="GB-Mo")]
    out = _with_sku(map_redshift, rows)
    assert out[0]["strategy"] == "passthrough"


def test_redshift_unknown_type_passthrough():
    rows = [row(product="Amazon Redshift",
                usage_type="UnknownNode", unit="Hrs")]
    out = _with_sku(map_redshift, rows)
    assert out[0]["strategy"] == "passthrough"


# ---------------------------------------------------------------------------
# Athena → BigQuery
# ---------------------------------------------------------------------------

def test_athena_data_scanned_maps_to_bq_analysis():
    # BigQuery's on-demand Analysis SKU is region-specific ("Analysis
    # (<region>)"), not one flat worldwide rate — a bare "Analysis" pattern
    # only resolves for ~2 US multi-region locations and silently under-prices
    # every other region by ~17%. gcp_sku_name now carries the resolved
    # region-qualified SKU name rather than the bare pattern, and
    # unit_multiplier does a real TB->TiB conversion (BigQuery bills per TiB,
    # Athena's CUR unit is decimal TB) instead of assuming the units match 1:1.
    rows = [row(product="Amazon Athena",
                usage_type="DataScanned-Bytes", unit="TB")]
    out = _with_sku(map_athena, rows)
    assert out[0]["gcp_service"] == "BigQuery"
    assert out[0]["gcp_sku_name"].startswith("Analysis")
    assert out[0]["strategy"] == "map"
    assert out[0]["unit_multiplier"] == pytest.approx(1e12 / (1024**4), rel=1e-6)


def test_athena_ddl_passthrough():
    rows = [row(product="Amazon Athena",
                usage_type="DDL-Queries", unit="Count")]
    out = _with_sku(map_athena, rows)
    assert out[0]["strategy"] == "passthrough"


# ---------------------------------------------------------------------------
# Kinesis shard-hours
# ---------------------------------------------------------------------------

def test_kinesis_shard_hours_passthrough_pubsub():
    rows = [row(product="Amazon Kinesis",
                usage_type="Kinesis-ShardHour", unit="Hrs")]
    out = map_kinesis(rows)
    assert out[0]["gcp_service"] == "Pub/Sub"
    assert out[0]["strategy"] == "passthrough"


# ---------------------------------------------------------------------------
# EFS → Filestore
# ---------------------------------------------------------------------------

def test_efs_standard_maps_to_basic_ssd():
    rows = [row(product="Amazon Elastic File System",
                usage_type="TimedStorage-EFS-ByteHrs", unit="GB-Mo")]
    out = _with_sku(map_efs, rows)
    assert out[0]["gcp_service"] == "Cloud Filestore"
    assert "SSD" in out[0]["gcp_sku_name"]


def test_efs_ia_maps_to_basic_hdd():
    rows = [row(product="Amazon Elastic File System",
                usage_type="TimedStorage-EFS-IA-ByteHrs", operation="Standard-IA", unit="GB-Mo")]
    out = _with_sku(map_efs, rows)
    assert "HDD" in out[0]["gcp_sku_name"]


def test_efs_provisioned_throughput_passthrough():
    rows = [row(product="Amazon Elastic File System",
                usage_type="ProvisionedThroughput-MBps", unit="MBps-Mo")]
    out = _with_sku(map_efs, rows)
    assert out[0]["strategy"] == "passthrough"


# ---------------------------------------------------------------------------
# FSx → Filestore
# ---------------------------------------------------------------------------

def test_fsx_lustre_maps_to_high_scale_ssd():
    rows = [row(product="Amazon FSx for Lustre",
                usage_type="FSx:Lustre-Storage", unit="GB-Mo")]
    out = _with_sku(map_fsx, rows)
    assert out[0]["gcp_service"] == "Cloud Filestore"
    assert "High Scale" in out[0]["gcp_sku_name"]


def test_fsx_windows_maps_to_enterprise():
    rows = [row(product="Amazon FSx for Windows File Server",
                usage_type="FSx:Windows-HDD-Storage", unit="GB-Mo")]
    out = _with_sku(map_fsx, rows)
    assert "Enterprise" in out[0]["gcp_sku_name"]


def test_fsx_ontap_passthrough():
    rows = [row(product="Amazon FSx for NetApp ONTAP",
                usage_type="FSx:ONTAP-Storage", unit="GB-Mo")]
    out = _with_sku(map_fsx, rows)
    assert out[0]["strategy"] == "passthrough"


def test_fsx_backup_passthrough():
    rows = [row(product="Amazon FSx",
                usage_type="FSx:Backup-GB-Mo", unit="GB-Mo")]
    out = _with_sku(map_fsx, rows)
    assert out[0]["strategy"] == "passthrough"


# ---------------------------------------------------------------------------
# X-Ray → Cloud Trace
# ---------------------------------------------------------------------------

def test_xray_maps_to_cloud_trace():
    rows = [row(product="AWS X-Ray", usage_type="Traces-Stored-Count", unit="Count")]
    out = _with_sku(map_xray, rows)
    assert out[0]["gcp_service"] == "Cloud Trace"
    assert out[0]["strategy"] == "map"
    assert out[0]["unit_multiplier"] == 1.0


# ---------------------------------------------------------------------------
# EMR → Dataproc
# ---------------------------------------------------------------------------

def test_emr_vcpu_extraction_from_usage_type():
    assert _emr_vcpus("m5.xlarge-EMR-CORE", None) == 4
    assert _emr_vcpus("r5.4xlarge-EMR-MASTER", None) == 16
    assert _emr_vcpus("c5.9xlarge-EMR-TASK", None) == 36


def test_emr_vcpu_prefers_instance_vcpus_field():
    assert _emr_vcpus("m5.xlarge-EMR-CORE", 8) == 8


def test_emr_vcpu_unknown_returns_none():
    assert _emr_vcpus("x2.weird-EMR-CORE", None) is None


def test_emr_known_instance_maps_to_dataproc_premium():
    # Real cluster-mode management-fee SKU lives under service "Compute
    # Engine" (resource_group "Dataproc"), not the "Dataproc" service itself
    # (that's the Dataproc Serverless Batch billing service — confirmed via
    # find-sku.sh it has no cluster-management-fee SKU at all).
    rows = [row(product="Amazon Elastic MapReduce",
                usage_type="m5.xlarge-EMR-CORE", unit="Hrs")]
    out = _with_sku(map_emr, rows)
    assert out[0]["gcp_service"] == "Compute Engine"
    assert out[0]["gcp_sku_name"] == "Licensing Fee for Google Cloud Dataproc (CPU cost)"
    assert out[0]["unit_multiplier"] == 4.0
    assert out[0]["strategy"] == "map"


def test_emr_r5_4xlarge_is_16_vcpus():
    rows = [row(product="AmazonEMR",
                usage_type="r5.4xlarge-EMR-MASTER", unit="Hrs")]
    out = _with_sku(map_emr, rows)
    assert out[0]["unit_multiplier"] == 16.0


def test_emr_unknown_instance_type_passthrough():
    rows = [row(product="Amazon Elastic MapReduce",
                usage_type="x2.weird-EMR-CORE", unit="Hrs")]
    out = _with_sku(map_emr, rows)
    assert out[0]["strategy"] == "passthrough"
    assert out[0]["gcp_service"] == "Dataproc"


def test_emr_spot_row_passthrough():
    rows = [row(product="Amazon Elastic MapReduce",
                usage_type="m5.xlarge-EMR-CORE-Spot", unit="Hrs")]
    out = _with_sku(map_emr, rows)
    assert out[0]["strategy"] == "passthrough"


# ---------------------------------------------------------------------------
# Non-workload
# ---------------------------------------------------------------------------

def test_marketplace_passthrough():
    rows = [row(product="AWS Marketplace", operation="SaaS License", unit="Hrs")]
    out = map_non_workload(rows)
    assert out[0]["strategy"] == "passthrough"
    assert "Marketplace" in out[0]["gcp_service"]


# ---------------------------------------------------------------------------
# CloudWatch
# ---------------------------------------------------------------------------

def test_cloudwatch_log_bytes_maps_to_cloud_logging():
    # Stale expectation fixed: this predates the ingestion-SKU work and
    # expected a bare cost-parity passthrough, but log-volume rows resolve
    # to a real catalog SKU ("Log Storage cost", 143F-A1B0-E0BE — its real
    # tiered rate, despite the name, is 0-50 GiB free then $0.50/GiB, GCP's
    # actual ingestion pricing) via resolve_sku(), same as every other
    # deterministic mapper in this file — not a bare passthrough.
    rows = [row(product="Amazon CloudWatch",
                usage_type="LogBytes-Processed", unit="GB")]
    out = _with_sku(map_cloudwatch, rows)
    assert out[0]["gcp_service"] == "Cloud Logging"
    assert out[0]["strategy"] == "map"
    assert out[0]["gcp_sku_name"] == "Log Storage cost"


def test_cloudwatch_metrics_passthrough():
    rows = [row(product="Amazon CloudWatch",
                usage_type="MetricMonitorUsage", unit="Count")]
    out = _with_sku(map_cloudwatch, rows)
    assert out[0]["strategy"] == "passthrough"


def test_cloudwatch_custom_metrics_low_volume_ignored():
    # Real AWS CUR shape: usage_type "...CW:MetricMonitorUsage" + operation
    # "MetricStorage" (verified against the raw CUR pricing description text,
    # "$0.30 per metric-month for the first 10,000 metrics"). Small metric
    # counts stay under GCP's free tier under any normal sampling rate.
    rows = [row(product="AmazonCloudWatch",
                usage_type="APS3-CW:MetricMonitorUsage",
                operation="MetricStorage", unit="Metrics", total_usage=500.0)]
    out = _with_sku(map_cloudwatch, rows)
    assert out[0]["strategy"] == "ignore"
    assert out[0]["gcp_service"] == "Cloud Monitoring"


def test_cloudwatch_custom_metrics_high_volume_not_silently_zeroed():
    # Regression for job 6a561187: an 11,764-metric / $3,013 row was previously
    # missed by the "metric month" text-only regex (operation is "MetricStorage",
    # never that literal phrase) and fell through to the generic passthrough
    # bucket. It must now be recognized as custom-metrics, and because the count
    # is far above the "tens-to-hundreds" range the ignore/$0 assumption relies
    # on, it must NOT be silently zeroed either — it should route to review with
    # the AWS cost kept as a placeholder.
    rows = [row(product="AmazonCloudWatch",
                usage_type="APS3-CW:MetricMonitorUsage",
                operation="MetricStorage", unit="Metrics",
                total_usage=11764.42, aws_amortized_cost=3013.00)]
    out = _with_sku(map_cloudwatch, rows)
    assert out[0]["strategy"] == "review"
    assert out[0]["gcp_service"] == "Cloud Monitoring"
    assert out[0]["unit_multiplier"] == 1.0


# ---------------------------------------------------------------------------
# EKS cluster-hours → GKE Zonal Cluster Management Fee
# ---------------------------------------------------------------------------

def test_eks_cluster_hours_maps_to_gke():
    rows = [row(product="Amazon Elastic Container Service for Kubernetes CreateOperation",
                usage_type="", unit="Hrs")]
    out = _with_sku(map_flat_hourly, rows)
    assert len(out) == 1
    assert out[0]["gcp_service"] == "Kubernetes Engine"
    assert out[0]["strategy"] == "map"
    assert "Zonal Kubernetes Clusters" in (out[0].get("gcp_sku_name") or "")


# ---------------------------------------------------------------------------
# Lambda APS3 classifier guard (must NOT land in object_storage)
# ---------------------------------------------------------------------------

def test_lambda_aps3_not_classified_as_object_storage():
    """Lambda product strings containing 'APS3' must not match the S3 object_storage rule."""
    from classify_mechanics import classify
    r = {
        "product": "AWS Lambda APS3-Lambda-GB-Second",
        "usage_type": "",
        "unit": "GB-Mo",
        "operation": "",
        "pricing_unit": "GB-Mo",
        "aws_amortized_cost": 500.0,
    }
    group = classify(r)
    assert group != "object_storage", (
        f"Lambda APS3 row misrouted to object_storage (got {group!r}); "
        "add Lambda exclusion to object_storage classifier"
    )


# ---------------------------------------------------------------------------
# Burstable EC2 (t-family → E2)
# ---------------------------------------------------------------------------

def test_burstable_t4g_stays_native_arm_not_sustained_c4a():
    """t4g (ARM/Graviton burstable) must default to T2A Arm (burstable ARM),
    never C4A Arm (sustained ARM — a real performance-tier violation for a
    burstable source) and never silently cross-architected to x86 E2 either.

    A prior version of this test asserted t4g must map to E2 — that was a
    real bug: ARM vs x86 is a genuine binary-compatibility boundary (a
    customer's actual deployed Graviton binaries don't run on x86 without a
    rebuild), the same principle this codebase applies everywhere else
    (never silently cross architectures, only disclose). Confirmed the
    real, severe version of the bug this masked: map_compute_burstable() is
    the ACTUAL, primary handler for burstable rows (classify_mechanics.py
    routes t2/t3/t3a/t4g here before family_mapper.py ever sees them), and
    it was hardcoded to archs=("x86",) regardless of the source's own
    architecture — every real t4g row was being silently force-mapped onto
    x86 with no disclosure at all. x86 E2 may still be surfaced as a
    disclosed, non-default option when it's genuinely cheaper — never
    silently switched to."""
    rows = [row(product="Amazon EC2",
                usage_type="APS5-BoxUsage:t4g.medium",
                unit="Hrs", instance_type="t4g.medium")]
    out = _with_sku(map_compute_burstable, rows)
    comps = {r["component"] for r in out}
    assert comps == {"core", "ram"}
    for r in out:
        assert r["gcp_service"] == "Compute Engine"
        assert r["strategy"] == "map"  # SKU resolved → both components mapped
        assert "T2A" in r["gcp_sku_name"]  # native ARM burstable family, not E2/C4A
        assert "C4A" not in r["projection_note"]  # never the sustained ARM tier

def test_burstable_t3_medium_vcpu_and_ram():
    """t3.medium → 2 vCPU, 4 GiB → unit_multiplier=2 (core) and 4 (ram)."""
    rows = [row(product="Amazon EC2",
                usage_type="BoxUsage:t3.medium",
                unit="Hrs", instance_type="t3.medium")]
    out = _with_sku(map_compute_burstable, rows)
    core = next(r for r in out if r["component"] == "core")
    ram  = next(r for r in out if r["component"] == "ram")
    assert core["unit_multiplier"] == 2.0
    assert ram["unit_multiplier"]  == 4.0

def test_burstable_classifier_t4g_does_not_land_in_compute_arm():
    """t4g must land in compute_burstable, not compute_arm."""
    from classify_mechanics import classify
    r = {
        "product": "Amazon Elastic Compute Cloud",
        "usage_type": "APS5-BoxUsage:t4g.medium",
        "unit": "Hrs",
        "operation": "",
        "pricing_unit": "Hrs",
        "aws_amortized_cost": 100.0,
    }
    group = classify(r)
    assert group == "compute_burstable", (
        f"t4g misrouted to {group!r}; should be compute_burstable"
    )
