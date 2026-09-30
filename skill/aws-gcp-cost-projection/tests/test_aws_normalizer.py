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
