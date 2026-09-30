"""
Layer 1 (aws_normalizer.py) and service_classifier.py regression tests.

Real AWS CUR bills commonly give the `product` field as the compact
concatenated form ("AmazonSES", "AmazonSNS", ...) rather than the spelled-out
name ("Amazon Simple Email Service"). Both canonical_service() and
service_classifier._norm() used to require a literal space after the
"Amazon"/"AWS" prefix before stripping it, which silently failed to
normalize every short-code compact-form product name — confirmed on a real
bill where "AmazonSES" (product field, verbatim) resolved to no canonical
service at all, so its $1,789 charge fell through the SES service_map rule
into the generic "no GCP equivalent found" bucket instead.
"""
import json
import os

from aws_normalizer import canonical_service
from service_classifier import _norm, _rule_matches

_SERVICE_MAP_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "service_map.json"
)


def _rules():
    return [r for r in json.load(open(_SERVICE_MAP_PATH)).get("rules", []) if r.get("match")]


def test_canonical_service_handles_compact_product_names():
    # These are real, unmodified AWS CUR product-field values.
    cases = {
        "AmazonSES": "ses",
        "AmazonSNS": "sns",
        "AmazonSQS": "sqs",
        "AmazonECS": "ecs",
        "AmazonEKS": "eks",
        "AmazonMSK": "msk",
        "AmazonKMS": "kms",
        "AmazonEBS": "ebs",
    }
    for product, expected in cases.items():
        assert canonical_service(product) == expected, product


def test_canonical_service_still_handles_spelled_out_names():
    assert canonical_service("Amazon Simple Email Service") == "ses"
    assert canonical_service("Amazon Elastic Compute Cloud") == "ec2"


def test_norm_strips_compact_prefix():
    assert _norm("AmazonSES") == "ses"
    assert _norm("AmazonMSK") == "msk"
    assert _norm("Amazon Simple Email Service") == "simple email service"


def test_ses_rule_matches_compact_product_name():
    rule = next(r for r in _rules() if "simple email" in r["match"])
    cs_product = canonical_service("AmazonSES")
    assert _rule_matches(rule, "AmazonSES", "APS3-Recipients-EC2", cs_product, {})


def test_sns_and_sqs_rules_match_compact_product_names():
    sns_rule = next(r for r in _rules() if "simple notification" in r["match"])
    sqs_rule = next(r for r in _rules() if "simple queue" in r["match"])
    assert _rule_matches(sns_rule, "AmazonSNS", "Requests-Tier1", canonical_service("AmazonSNS"), {})
    assert _rule_matches(sqs_rule, "AmazonSQS", "Requests-Tier1", canonical_service("AmazonSQS"), {})


def test_dms_product_name_recognized_as_native_aws_service():
    # Real AWS CUR product-field value (job 6a561187, $945.00 row): a native,
    # first-party AWS service ("AWS Database Migration Service") with no space
    # after the "AWS" prefix and no internal word separators at all
    # ("AWSDatabaseMigrationSvc" is one PascalCase-concatenated ProductCode).
    # canonical_service() previously returned None for it (no alias existed at
    # all, unlike SES/SNS/SQS which had aliases but failed normalization) —
    # which meant classify_mechanics.py's marketplace_thirdparty catch-all
    # rule (guarded by `canonical_service(product) is None`) misclassified a
    # genuine native AWS service as third-party marketplace spend, permanently
    # blocking it from the pre-existing, already-correct "database migration"
    # service_map rule (DMS -> Database Migration Service, flagged for $0
    # candidacy since GCP DMS is free for homogeneous migrations).
    assert canonical_service("AWSDatabaseMigrationSvc") == "dms"
    assert canonical_service("AWS Database Migration Service") == "dms"


def test_dms_rule_matches_and_is_not_marketplace_thirdparty():
    import re

    product = "AWSDatabaseMigrationSvc"
    cs_product = canonical_service(product)
    assert not (
        bool(product)
        and not re.match(r"^(amazon|aws)\b", product, re.IGNORECASE)
        and cs_product is None
    ), "DMS row would still be misclassified as marketplace_thirdparty"

    dms_rule = next(r for r in _rules() if r["match"] == "database migration")
    # Covers every DMS replication-instance size seen on job 6a561187 (r5.2xlarge
    # $945.00, r5.xlarge $262.35, r6i.xlarge $262.35) — the fix is keyed on the
    # product name, not the instance type, so all sizes must resolve alike.
    for usage_type in (
        "APS3-InstanceUsg:dms.r5.2xlarge",
        "APS3-InstanceUsg:dms.r5.xlarge",
        "APS3-InstanceUsg:dms.r6i.xlarge",
        "APS3-InstanceUsg:dms.t3.large",
        "APS3-InstanceUsg:dms.t3.medium",
    ):
        assert _rule_matches(dms_rule, product, usage_type, cs_product, {}), usage_type


def test_sagemaker_product_name_recognized_as_native_aws_service():
    # Real AWS CUR product-field value (job 6a561187, $676.17 row): another
    # native AWS service with no space after the "Amazon" prefix
    # ("AmazonSageMaker"), same missing-alias bug class as DMS — canonical_
    # service() returned None (no alias existed), so classify_mechanics.py's
    # marketplace_thirdparty catch-all misclassified it as third-party SaaS,
    # blocking the pre-existing, already-correct "sagemaker" service_map rule
    # (SageMaker -> Vertex AI, review).
    assert canonical_service("AmazonSageMaker") == "sagemaker"


def test_sagemaker_rule_matches_and_is_not_marketplace_thirdparty():
    import re

    product = "AmazonSageMaker"
    cs_product = canonical_service(product)
    assert not (
        bool(product)
        and not re.match(r"^(amazon|aws)\b", product, re.IGNORECASE)
        and cs_product is None
    ), "SageMaker row would still be misclassified as marketplace_thirdparty"

    sm_rule = next(r for r in _rules() if r["match"] == "sagemaker")
    assert _rule_matches(
        sm_rule, product, "APS3-MLflow:TrackingServerCompute-Small", cs_product, {}
    )


def test_rds_mysql_community_edition_rule_does_not_zero_generic_rds_rows():
    # Regression for job d01fb92d (row 7, $6,783.08): the service_map rule
    # "relational database service for mysql community edition" (mode=ignore,
    # meant ONLY for AWS's distinct "RDS for MySQL Community Edition" EOL-
    # surcharge product) was reachable via canonical_service-equivalence for
    # ANY plain "Amazon Relational Database Service" row — because
    # canonical_service()'s substring fallback found the shorter embedded
    # alias "relational database service" (-> "rds") inside the rule's own,
    # longer match text, collapsing a narrow rule onto the generic RDS key.
    # Since mechanic_group "managed_db" isn't in DETERMINISTIC_GROUPS, this
    # silently zeroed a genuine $6,783.08 Aurora Serverless v2 I/O-Optimized
    # compute charge, mislabeling it as a "MySQL 5.7 EOL surcharge" it had
    # nothing to do with. Fixed by giving the rule's own match text a
    # dedicated alias key distinct from the generic "rds" one.
    generic_rds_cs = canonical_service("Amazon Relational Database Service")
    rule_text_cs = canonical_service(
        "relational database service for mysql community edition"
    )
    assert generic_rds_cs == "rds"
    assert rule_text_cs != generic_rds_cs

    mysql_rule = next(
        r
        for r in _rules()
        if r["match"] == "relational database service for mysql community edition"
    )
    assert not _rule_matches(
        mysql_rule,
        "Amazon Relational Database Service",
        "APS3-Aurora:ServerlessV2IOOptimizedUsage",
        generic_rds_cs,
        {},
    ), "generic RDS/Aurora row must not match the MySQL-Community-Edition-only rule"

    # The genuine target product must still match correctly.
    specific_cs = canonical_service(
        "Amazon Relational Database Service for MySQL Community Edition"
    )
    assert _rule_matches(
        mysql_rule,
        "Amazon Relational Database Service for MySQL Community Edition",
        "APS3-ExtendedSupport:Yr1-Yr2:MySQL5.7",
        specific_cs,
        {},
    )
