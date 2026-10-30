#!/usr/bin/env python3
"""
Parity + unit tests for scripts/spec_dispatch.py.

GOAL: prove the per-service spec dispatch engine reproduces the mechanic_group
that classify_mechanics.classify() assigns today, for every EC2/RDS charge-type.
A green run means service_specs.json (ec2+rds) is a faithful drop-in for the
EC2/RDS portion of the legacy rule chain — the prerequisite for cutover.

Runnable two ways:
  pytest tests/test_spec_dispatch.py -v
  python  tests/test_spec_dispatch.py          (prints a parity table)
"""
from __future__ import annotations
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _SCRIPTS)

import classify_mechanics as cm
import spec_dispatch as sd
from aws_normalizer import canonical_service


def _row(**kw) -> dict:
    """Build a row dict with the same keys classify_mechanics.main() loads,
    and pre-resolve canonical identity exactly like main() does."""
    r = {
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
        "aws_region": kw.get("aws_region", "ap-south-1"),
    }
    r["canonical"] = canonical_service(r["product"], r["product_code"])
    return r


# (name, row, expected_mechanic_group, expected_charge_type_or_None)
CASES = [
    # ---- EC2 ----
    ("ec2_x86_ondemand",
     _row(product="Amazon Elastic Compute Cloud", product_code="AmazonEC2",
          usage_type="APS3-BoxUsage:m5.xlarge", operation="RunInstances",
          unit="Hrs", instance_type="m5.xlarge"),
     # instance_hour is now a GROUP; plain x86 rows land on its default child.
     # Mechanic is unchanged — only the charge_type id is more specific.
     "compute_breakdown", "instance_hour_standard"),
    ("ec2_windows",
     _row(product="Amazon Elastic Compute Cloud", product_code="AmazonEC2",
          usage_type="APS3-BoxUsage:m5.xlarge", operation="RunInstances:0002 Windows",
          unit="Hrs", instance_type="m5.xlarge", operating_system="Windows"),
     "compute_windows", "instance_hour_windows"),
    ("ec2_burstable_t3",
     _row(product="Amazon Elastic Compute Cloud", product_code="AmazonEC2",
          usage_type="APS3-BoxUsage:t3.medium", operation="RunInstances",
          unit="Hrs", instance_type="t3.medium"),
     "compute_burstable", "instance_hour_burstable"),
    ("ec2_graviton_m7g",
     _row(product="Amazon Elastic Compute Cloud", product_code="AmazonEC2",
          usage_type="APS3-BoxUsage:m7g.xlarge", operation="RunInstances",
          unit="Hrs", instance_type="m7g.xlarge"),
     "compute_arm", "instance_hour_graviton"),
    ("ec2_spot_c5",
     _row(product="Amazon Elastic Compute Cloud", product_code="AmazonEC2",
          usage_type="APS3-SpotUsage:c5.xlarge", operation="RunInstances",
          unit="Hrs", pricing_model="Spot", instance_type="c5.xlarge"),
     "compute_breakdown", "instance_hour_standard"),
    ("ec2_ebs_gp3",
     _row(product="Amazon Elastic Compute Cloud", product_code="AmazonEC2",
          usage_type="APS3-EBS:VolumeUsage.gp3", operation="", unit="GB-Mo",
          volume_type="gp3"),
     "block_storage", "ebs_storage"),
    ("ec2_data_transfer_out",
     _row(product="Amazon Elastic Compute Cloud", product_code="AmazonEC2",
          usage_type="APS3-DataTransfer-Out-Bytes", operation="", unit="GB"),
     "data_transfer", "data_transfer"),
    ("ec2_nat_hours",
     _row(product="Amazon Elastic Compute Cloud", product_code="AmazonEC2",
          usage_type="APS3-NatGateway-Hours", operation="", unit="Hrs"),
     "flat_hourly", "nat_and_flat_hourly"),
    ("ec2_cpu_credits",
     _row(product="Amazon Elastic Compute Cloud", product_code="AmazonEC2",
          usage_type="APS3-CPUCredits:t3", operation="", unit="Hrs"),
     "non_workload", None),   # caught by global pre-filter
    ("ec2_negative_credit",
     _row(product="Amazon Elastic Compute Cloud", product_code="AmazonEC2",
          usage_type="APS3-BoxUsage:m5.xlarge", operation="RunInstances",
          unit="Hrs", aws_amortized_cost=-50.0, instance_type="m5.xlarge"),
     "negative_cost", None),  # caught by global pre-filter

    # ---- RDS ----
    ("rds_instance_hour",
     _row(product="Amazon Relational Database Service", product_code="AmazonRDS",
          usage_type="APS3-InstanceUsage:db.m5.large", operation="CreateDBInstance",
          unit="Hrs", database_engine="postgresql", deployment_option="Single-AZ"),
     "managed_db", "instance_hour"),
    ("rds_aurora_serverless_v2",
     _row(product="Amazon Relational Database Service", product_code="AmazonRDS",
          usage_type="APS3-Aurora:ServerlessV2Usage", operation="", unit="ACU-Hr",
          database_engine="aurora-postgresql"),
     "managed_db", "instance_hour"),
    ("rds_storage_gp2",
     _row(product="Amazon Relational Database Service", product_code="AmazonRDS",
          usage_type="APS3-RDS:GP2-Storage", operation="", unit="GB-Mo",
          volume_type="gp2"),
     "block_storage", "storage"),
    ("rds_provisioned_iops",
     _row(product="Amazon Relational Database Service", product_code="AmazonRDS",
          usage_type="APS3-RDS:GP3-PIOPS",
          operation="provisioned IOPS-month of GP3 IOPS", unit="IOPS-Mo"),
     "block_storage", "provisioned_iops"),
    ("rds_extended_support",
     _row(product="Amazon Relational Database Service", product_code="AmazonRDS",
          usage_type="APS3-ExtendedSupport:Yr1-Yr2:MySQL8.0", operation="",
          unit="vCPU-Hrs", database_engine="mysql"),
     "rds_extended_support", "extended_support"),
    ("rds_backup",
     _row(product="Amazon Relational Database Service", product_code="AmazonRDS",
          usage_type="APS3-RDS:ChargedBackupUsage", operation="backup storage",
          unit="GB-Mo"),
     "block_storage", "backup"),

    # ---- S3 ----
    ("s3_storage_standard",
     _row(product="Amazon Simple Storage Service", product_code="AmazonS3",
          usage_type="APS3-TimedStorage-ByteHrs", operation="StandardStorage", unit="GB-Mo"),
     "object_storage", "s3_storage"),
    ("s3_get_requests",
     _row(product="Amazon Simple Storage Service", product_code="AmazonS3",
          usage_type="APS3-Requests-Tier1", operation="GetObject", unit="Requests"),
     "per_request", "s3_requests"),
    ("s3_data_transfer_out",
     _row(product="Amazon Simple Storage Service", product_code="AmazonS3",
          usage_type="APS3-DataTransfer-Out-Bytes", unit="GB"),
     "data_transfer", "s3_data_transfer"),

    # ---- AWS Data Transfer ----
    ("datatransfer_internet",
     _row(product="AWS Data Transfer", product_code="AWSDataTransfer",
          usage_type="APS3-DataTransfer-Out-Bytes", unit="GB"),
     "data_transfer", "data_transfer"),

    # ---- OpenSearch ----
    ("opensearch_instance",
     _row(product="Amazon OpenSearch Service", product_code="AmazonES",
          usage_type="APS3-ESInstance:r6g.large.search", unit="Hrs"),
     "opensearch", "opensearch_compute"),
    ("opensearch_data_transfer",
     _row(product="Amazon OpenSearch Service", product_code="AmazonES",
          usage_type="APS3-DataTransfer-Out-Bytes", unit="GB"),
     "data_transfer", "opensearch_data_transfer"),
    ("opensearch_storage",
     _row(product="Amazon OpenSearch Service", product_code="AmazonES",
          usage_type="APS3-ES:Storage:gp3", unit="GB-Mo"),
     "block_storage", "opensearch_storage"),

    # ---- Redshift ----
    ("redshift_node",
     _row(product="Amazon Redshift", product_code="AmazonRedshift",
          usage_type="APS3-Node:ra3.4xlarge", unit="Hrs"),
     "redshift", "redshift_all"),

    # ---- ElastiCache ----
    ("elasticache_node",
     _row(product="Amazon ElastiCache", product_code="AmazonElastiCache",
          usage_type="APS3-NodeUsage:cache.r6g.large", unit="Hrs"),
     "elasticache", "elasticache_all"),

    # ---- MSK ----
    ("msk_broker_hours",
     _row(product="Amazon Managed Streaming for Apache Kafka", product_code="AmazonMSK",
          usage_type="APS3-Kafka.m5.large", operation="broker hour", unit="Hrs"),
     "msk", "msk_broker_hours"),
    ("msk_storage",
     _row(product="Amazon Managed Streaming for Apache Kafka", product_code="AmazonMSK",
          usage_type="APS3-Kafka.Storage", unit="GB-Mo"),
     "block_storage", "msk_storage"),

    # ---- EMR ----
    ("emr_fee",
     _row(product="Amazon Elastic MapReduce",
          usage_type="APS3-BoxUsage:m5.xlarge", unit="Hrs"),
     "emr", "emr_all"),

    # ---- Athena ----
    ("athena_scan",
     _row(product="Amazon Athena", product_code="AmazonAthena",
          usage_type="APS3-DataScannedInTB", unit="TB"),
     "athena", "athena_all"),

    # ---- Glue ----
    ("glue_dpu_hours",
     _row(product="AWS Glue", product_code="AWSGlue",
          usage_type="APS3-ETL-DPU-Hour", unit="DPU-Hour"),
     "glue", "glue_dpu_hours"),
    ("glue_catalog_fallback",
     _row(product="AWS Glue", product_code="AWSGlue",
          usage_type="APS3-Catalog-Requests", unit="Requests"),
     "per_request", "glue_catalog_requests"),

    # ---- Kinesis ----
    ("kinesis_shard_hours",
     _row(product="Amazon Kinesis", product_code="AmazonKinesis",
          usage_type="APS3-Kinesis-Shard-Hour", unit="Hrs"),
     "kinesis", "kinesis_shard_hours"),
    ("kinesis_data_volume",
     _row(product="Amazon Kinesis", product_code="AmazonKinesis",
          usage_type="APS3-Kinesis-PutPayloadUnits", unit="Requests"),
     "per_request", "kinesis_data_volume"),

    # ---- QuickSight ----
    ("quicksight_user",
     _row(product="Amazon QuickSight", product_code="AmazonQuickSight",
          usage_type="APS3-QuickSight-Enterprise-Author", unit=""),
     "quicksight", "quicksight_all"),
    ("quicksight_count_yields_per_request",
     _row(product="Amazon QuickSight", product_code="AmazonQuickSight",
          usage_type="APS3-QuickSight-Session", unit="Count"),
     "per_request", "quicksight_requests"),

    # ---- Shield ----
    ("shield_monthly_fee",
     _row(product="AWS Shield", product_code="AWSShield",
          usage_type="APS3-Shield-Monthly-Fee", unit=""),
     "shield", "shield_monthly_fee"),

    # ---- DynamoDB ----
    ("dynamodb_storage",
     _row(product="Amazon DynamoDB", product_code="AmazonDynamoDB",
          usage_type="APS3-TimedStorage-ByteHrs", unit="GB-Mo"),
     "dynamodb_storage", "dynamodb_storage"),
    ("dynamodb_wcu_fallback",
     _row(product="Amazon DynamoDB", product_code="AmazonDynamoDB",
          usage_type="APS3-WriteRequestUnits", unit="Count"),
     "misc", "dynamodb_throughput"),  # excluded from per_request; routes to misc via dynamodb_throughput

    # ---- EFS ----
    ("efs_storage",
     _row(product="Amazon Elastic File System", product_code="AmazonEFS",
          usage_type="APS3-TimedStorage-ByteHrs", unit="GB-Mo"),
     "efs", "efs_all"),

    # ---- FSx ----
    ("fsx_storage",
     _row(product="Amazon FSx", product_code="AmazonFSx",
          usage_type="APS3-FSx-Storage", unit="GB-Mo"),
     "fsx", "fsx_all"),

    # ---- GuardDuty / Security Hub ----
    ("guardduty_analyzed",
     _row(product="Amazon GuardDuty", product_code="AmazonGuardDuty",
          usage_type="APS3-PaidEventsAnalyzed-Bytes", unit="GB"),
     "guardduty", "guardduty_all"),
    ("securityhub_findings",
     _row(product="AWS Security Hub",
          usage_type="APS3-FindingsIngested", unit="Count"),
     "guardduty", None),  # canonical may route via spec or legacy; mechanic is guardduty either way

    # ---- Inspector ----
    ("inspector_scan",
     _row(product="Amazon Inspector", product_code="AmazonInspector",
          usage_type="APS3-Inspector-EC2-Scanning", unit=""),
     "inspector", "inspector_all"),
    ("inspector_count_yields_per_request",
     _row(product="Amazon Inspector", product_code="AmazonInspector",
          usage_type="APS3-Inspector-ImagesScanned", unit="Count"),
     "per_request", "inspector_requests"),

    # ---- X-Ray ----
    ("xray_traces",
     _row(product="AWS X-Ray", product_code="AWSXRay",
          usage_type="APS3-XRay-TracesRecorded", unit="Count"),
     "xray", "xray_all"),

    # ---- CloudWatch ----
    ("cloudwatch_logs",
     _row(product="AmazonCloudWatch", product_code="AmazonCloudWatch",
          usage_type="APS3-PutLogEvents-Bytes", unit="GB"),
     "cloudwatch", "cloudwatch_all"),

    # ---- MWAA ----
    ("mwaa_env",
     _row(product="Amazon Managed Workflows for Apache Airflow", product_code="AmazonMWAA",
          usage_type="APS3-Environment-mw1.small", unit="Hrs"),
     "mwaa", "mwaa_all"),

    # ---- Route 53 ----
    ("route53_queries",
     _row(product="Amazon Route 53", product_code="AmazonRoute53",
          usage_type="APS3-DNS-Queries", unit="Queries"),
     "per_request", "route53_dns"),

    # ---- EKS ----
    ("eks_cluster_hours",
     _row(product="Amazon Elastic Container Service for Kubernetes", product_code="AmazonEKS",
          usage_type="APS3-AmazonEKS-Hours:perCluster", unit="Hours"),
     "flat_hourly", "eks_cluster_hours"),

    # ---- Lambda ----
    ("lambda_gb_second",
     _row(product="AWS Lambda", product_code="AWSLambda",
          usage_type="APS3-Lambda-GB-Second", unit="Lambda-GB-Second"),
     "per_request", "lambda_metered"),
    ("lambda_requests",
     _row(product="AWS Lambda", product_code="AWSLambda",
          usage_type="APS3-Request", unit="Requests"),
     "per_request", "lambda_metered"),
    ("lambda_ephemeral_storage_fallback",
     _row(product="AWS Lambda", product_code="AWSLambda",
          usage_type="APS3-Lambda-Storage-Duration", unit="GB-Mo"),
     "misc", None),  # excluded from block/object storage & per_request -> misc

    # ---- ELB ----
    ("elb_hours",
     _row(product="Elastic Load Balancing", product_code="AWSELB",
          usage_type="APS3-LoadBalancerUsage", unit="Hrs"),
     "flat_hourly", "elb_hours"),
    ("elb_lcu",
     _row(product="Elastic Load Balancing", product_code="AWSELB",
          usage_type="APS3-LCUUsage", unit="LCU-Hrs"),
     "data_transfer", "elb_data"),

    # ---- SQS / SNS / API Gateway ----
    ("sqs_requests",
     _row(product="Amazon Simple Queue Service",
          usage_type="APS3-Requests-Tier1", unit="Requests"),
     "per_request", "sqs_requests"),
    ("sns_requests",
     _row(product="Amazon Simple Notification Service", product_code="AmazonSNS",
          usage_type="APS3-Requests-Tier1", unit="Requests"),
     "per_request", "sns_requests"),
    ("apigateway_calls",
     _row(product="Amazon API Gateway", product_code="AmazonApiGateway",
          usage_type="APS3-ApiGatewayRequest", unit="Requests"),
     "per_request", "apigateway_requests"),

    # ---- Direct Connect ----
    ("directconnect_port_hours",
     _row(product="AWS Direct Connect",
          usage_type="APS3-DirectConnect:Port", unit="Hrs"),
     "flat_hourly", "dc_port_hours"),
    ("directconnect_data",
     _row(product="AWS Direct Connect",
          usage_type="APS3-DataTransfer-Out-Bytes", unit="GB"),
     "data_transfer", "dc_data"),

    # ---- Global Accelerator ----
    ("globalaccelerator_fee",
     _row(product="AWS Global Accelerator",
          usage_type="APS3-GlobalAccelerator-fixed-fee", unit="accelerator-hours"),
     "flat_hourly", "ga_fixed_fee"),

    # ---- WAF ----
    ("waf_webacl_hour",
     _row(product="AWS WAF", product_code="awswaf",
          usage_type="APS3-WebACL-Hour", unit="Hrs"),
     "flat_hourly", "waf_fixed_hourly"),
    ("waf_request",
     _row(product="AWS WAF", product_code="awswaf",
          usage_type="APS3-RequestV2-Tier1", unit="Requests"),
     "per_request", "waf_requests"),
    ("waf_monthly",
     _row(product="AWS WAF", product_code="awswaf",
          usage_type="APS3-BotControl", operation="BotControl fee per Month", unit=""),
     "flat_hourly", "waf_monthly"),

    # ---- MemoryDB / DocumentDB ----
    ("memorydb_instance",
     _row(product="Amazon MemoryDB",
          usage_type="APS3-NodeUsage:db.r6g.large", unit="Hrs"),
     "managed_db", "instance_hour"),
    ("memorydb_storage",
     _row(product="Amazon MemoryDB",
          usage_type="APS3-MemoryDB-Storage", unit="GB-Mo"),
     "block_storage", "storage"),
    ("documentdb_instance",
     _row(product="Amazon DocumentDB",
          usage_type="APS3-InstanceUsage:db.r5.large", unit="Hrs"),
     "managed_db", "instance_hour"),

    # ---- misc-routed / downstream-corrected services (with fields) ----
    ("cognito_mau",
     _row(product="Amazon Cognito", usage_type="APS3-CognitoMAU", unit="Count"),
     "misc", "cognito_all"),
    ("ses_emails",
     _row(product="Amazon Simple Email Service", product_code="AmazonSES",
          usage_type="APS3-Message", unit="Count"),
     "misc", "ses_all"),
    ("sagemaker_notebook",
     _row(product="Amazon SageMaker", usage_type="APS3-Notebook:ml.t3.medium", unit="Hrs"),
     "misc", "sagemaker_all"),
    ("dms_instance",
     _row(product="AWS Database Migration Service",
          usage_type="APS3-CreateDMSReplicationInstance:dms.r5.large", unit="Hrs"),
     "misc", "dms_all"),
    ("backup_storage",
     _row(product="AWS Backup", usage_type="APS3-BackupStorage", unit="GB-Mo"),
     "block_storage", "backup_storage"),
    ("cloudtrail_events",
     _row(product="AWS CloudTrail", usage_type="APS3-PaidEventsRecorded", unit="Count"),
     "per_request", "cloudtrail_events"),
    ("cloudtrail_blank",
     _row(product="AWS CloudTrail", usage_type="APS3-FreeEventsRecorded", unit=""),
     "misc", "cloudtrail_other"),
    ("ssm_parameter_store_name_only",
     _row(product="AWS Systems Manager", usage_type="APS3-PS-Advanced-Param", unit="Hrs"),
     "ssm_parameter_store", "ssm_parameter_store"),
    ("vpc_tgw_hours",
     _row(product="Amazon Virtual Private Cloud", product_code="AmazonVPC",
          usage_type="APS3-TransitGateway-Hours", unit="Hrs"),
     "flat_hourly", "vpc_tgw_hours"),
    ("vpc_tgw_bytes",
     _row(product="Amazon Virtual Private Cloud", product_code="AmazonVPC",
          usage_type="APS3-TransitGateway-Bytes", unit="GB"),
     "data_transfer", "vpc_tgw_data"),
    ("vpc_endpoint",
     _row(product="Amazon Virtual Private Cloud", product_code="AmazonVPC",
          usage_type="APS3-VpcEndpoint-Hours", unit="Hrs"),
     "misc", "vpc_other"),
    # ---- SSM ----
    ("ssm_parameter_store_with_code",
     _row(product="AWS Systems Manager", product_code="AWSSystemsManager",
          usage_type="APS3-PS-Advanced-Param", unit="Months"),
     "ssm_parameter_store", "ssm_parameter_store"),
    ("ssm_other",
     _row(product="AWS Systems Manager", product_code="AWSSystemsManager",
          usage_type="APS3-Automation-Step", unit="Steps"),
     "misc", "ssm_other"),
    # ---- ECS & Fargate ----
    ("ecs_fargate_vcpu",
     _row(product="Amazon Elastic Container Service", product_code="AmazonECS",
          usage_type="APS3-Fargate-vCPU-Hours:perCPU", unit="Hrs"),
     "per_request", "ecs_fargate_vcpu"),
    ("ecs_fargate_memory",
     _row(product="Amazon Elastic Container Service", product_code="AmazonECS",
          usage_type="APS3-Fargate-GB-Hours", unit="GB-Hours"),
     "per_request", "ecs_fargate_memory"),
    ("fargate_memory",
     _row(product="Amazon Elastic Container Service APS3-Fargate-GB-Hours",
          usage_type="APS3-Fargate-GB-Hours", unit="GB-Hours"),
     "per_request", "fargate_memory"),
    # ---- ECR ----
    ("ecr_storage",
     _row(product="Amazon Elastic Container Registry", product_code="AmazonECR",
          usage_type="APS3-ECR-ByteHrs", unit="GB-Mo"),
     "misc", "ecr_storage"),
    ("ecr_data_transfer",
     _row(product="Amazon Elastic Container Registry", product_code="AmazonECR",
          usage_type="APS3-DataTransfer-Out-Bytes", unit="GB"),
     "data_transfer", "ecr_data_transfer"),
    # ---- KMS ----
    ("kms_keys",
     _row(product="AWS Key Management Service", product_code="awskms",
          usage_type="APS3-KMS-Keys", unit="Key-Months"),
     # per_request (not misc): classify_mechanics now routes KMS to the
     # deterministic key-month/operations split in map_per_request, so the
     # spec and the legacy classifier agree here.
     "per_request", "kms_keys"),
    ("kms_requests",
     _row(product="AWS Key Management Service", product_code="awskms",
          usage_type="APS3-KMS-Requests", unit="Requests"),
     "per_request", "kms_requests"),
    # ---- Secrets Manager ----
    ("secretsmanager_secrets",
     _row(product="AWS Secrets Manager", product_code="AWSSecretsManager",
          usage_type="APS3-Secret-Mo", unit="Secret-Months"),
     "misc", "secretsmanager_secrets"),
    ("secretsmanager_api",
     _row(product="AWS Secrets Manager", product_code="AWSSecretsManager",
          usage_type="APS3-APIRequests", unit="Requests"),
     "per_request", "secretsmanager_api"),
    # ---- Step Functions ----
    ("states_transitions",
     _row(product="AWS Step Functions", product_code="AmazonStates",
          usage_type="APS3-StateTransition", unit="Transitions"),
     "misc", "states_transitions"),
    # ---- Certificate Manager ----
    ("acm_private_ca",
     _row(product="AWS Certificate Manager", product_code="AWSCertificateManager",
          usage_type="APS3-PrivateCA-Active", unit="Months"),
     "misc", "acm_private_ca"),
    # ---- Bedrock ----
    ("bedrock_tokens",
     _row(product="Amazon Bedrock", product_code="AmazonBedrock",
          usage_type="APS3-Titan-Text-G1-Tokens", unit="1000 Tokens"),
     "misc", "bedrock_tokens"),
    # ---- EventBridge ----
    ("events_requests",
     _row(product="Amazon EventBridge", product_code="AWSEvents",
          usage_type="APS3-CustomEvents", unit="Requests"),
     "per_request", "events_requests"),
    ("events_other",
     _row(product="Amazon EventBridge", product_code="AWSEvents",
          usage_type="APS3-CustomEvents", unit="Events"),
     "misc", "events_other"),
    # ---- AppSync ----
    ("appsync_requests",
     _row(product="AWS AppSync", product_code="AWSAppSync",
          usage_type="APS3-Query-Requests", unit="Requests"),
     "per_request", "appsync_requests"),
    # ---- Amazon MQ ----
    ("mq_broker_hours",
     _row(product="Amazon MQ", product_code="AmazonMQ",
          usage_type="APS3-Broker-Hours:mq.t3.micro", unit="Hrs"),
     "misc", "mq_broker_hours"),
    # ---- Deliberate improvements over legacy misc (free services -> $0 ignore).
    #      These DIVERGE from classify_mechanics (which returns misc) on purpose;
    #      registered in EXPECTED_DIVERGENCES below so shadow-diff treats the
    #      cutover flip (misc -> ignore) as intended, not a regression. ----
    ("ecs_management",
     _row(product="Amazon Elastic Container Service", product_code="AmazonECS",
          usage_type="APS3-ECS-Cluster-Hours", operation="ECS cluster management", unit="Hrs"),
     "ignore", "ecs_management"),
    ("acm_public_cert",
     _row(product="AWS Certificate Manager", product_code="AWSCertificateManager",
          usage_type="APS3-PublicCertificate", unit=""),
     "ignore", "acm_public_cert"),
]


# ---------------------------------------------------------------------------
# pytest entry points
# ---------------------------------------------------------------------------
import pytest


# Cases that deliberately diverge from legacy classify_mechanics:
# - NEW-4: greedy service rules swallowed egress/storage rows into the service
#   mechanic; service_specs.json yields them to shared data_transfer/block_storage.
# - Registry free-service improvements: ACM public certs and the ECS control plane
#   are free on AWS (and GCP), so the spec routes them to `ignore` ($0) instead of
#   the legacy `misc`-guess. Correct improvement; registered so the misc->ignore
#   flip at cutover is treated as intended, not a regression.
EXPECTED_DIVERGENCES = {
    "opensearch_data_transfer": "data_transfer",
    "opensearch_storage": "block_storage",
    "ecs_management": "ignore",
    "acm_public_cert": "ignore",
}


@pytest.mark.parametrize("name,row,expected_group,expected_ct",
                         CASES, ids=[c[0] for c in CASES])
def test_parity_with_classify_mechanics(name, row, expected_group, expected_ct):
    viaspec = sd.classify_via_specs(row)
    assert viaspec == expected_group, (
        f"{name}: got {viaspec!r}, expected {expected_group!r}")
    if name in EXPECTED_DIVERGENCES:
        # Deliberate improvement over classify_mechanics (NEW-4 greedy service split).
        # See docs/WORKORDER_greedy_service_split.md and docs/SERVICE_SPECS_DESIGN.md.
        return
    legacy = cm.classify_via_prefilter(row) if hasattr(cm, "classify_via_prefilter") else _legacy_full(row)
    assert viaspec == legacy, (
        f"{name}: spec dispatch={viaspec!r} != legacy classify={legacy!r}")


@pytest.mark.parametrize("name,row,expected_group,expected_ct",
                         CASES, ids=[c[0] for c in CASES])
def test_charge_type_assignment(name, row, expected_group, expected_ct):
    if expected_ct is None:
        return  # global pre-filter cases have no per-service charge_type
    d = sd.dispatch(row)
    assert d is not None, f"{name}: dispatch returned None"
    assert d["charge_type"] == expected_ct, (
        f"{name}: charge_type={d['charge_type']!r}, expected {expected_ct!r}")


def _legacy_full(row: dict) -> str:
    """Oracle = exactly what classify_mechanics produces for a row: run the
    full RULES chain. (classify() already does this.)"""
    return cm.classify(row)


# ---------------------------------------------------------------------------
# script runner (no pytest needed)
# ---------------------------------------------------------------------------
def _run_cli() -> int:
    width = max(len(c[0]) for c in CASES)
    print(f"{'case':<{width}}  {'legacy':<22} {'via_specs':<22} {'charge_type':<22} ok")
    print("-" * (width + 72))
    failures = 0
    for name, row, expected_group, expected_ct in CASES:
        legacy = cm.classify(row)
        viaspec = sd.classify_via_specs(row)
        d = sd.dispatch(row)
        ct = d["charge_type"] if d else "-"
        if name in EXPECTED_DIVERGENCES:
            ok = (viaspec == expected_group) and (expected_ct is None or ct == expected_ct)
        else:
            ok = (viaspec == legacy == expected_group) and (expected_ct is None or ct == expected_ct)
        if not ok:
            failures += 1
        print(f"{name:<{width}}  {legacy:<22} {viaspec:<22} {ct:<22} {'OK' if ok else 'FAIL'}")
    print("-" * (width + 72))
    print(f"{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_cli())
