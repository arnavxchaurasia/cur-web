#!/usr/bin/env python3
"""
egress_rates.py — canonical GCP network-egress rates by direction.

Data-transfer egress cannot be resolved by fuzzy catalog SKU-name matching: the
catalog holds a region-pair explosion of "Network Inter Region Data Transfer Out
from <A> to <B>" SKUs, so a generic name like "inter-zone egress" word-overlap-
matches a different one each run (observed 2x on one run, 8x on the next — both
wrong). Egress pricing is stable and published, so we pin it to these canonical
rates instead: deterministic (same input → same rate) and correct.

Ingress is free on GCP and is handled upstream as strategy='ignore'.

Rates are tier-0 $/GB (published GCP list, verified against the bundled catalog):
  - inter-zone (between zones, same region):        $0.01/GB
  - inter-region (cross-region egress):             $0.08/GB  (catalog-observed)
  - internet egress (to the public internet):       $0.12/GB  (first tier)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import load_data_config as _cfg

_egress_cfg = _cfg("egress-rates").get("egress_skus", {})

# direction -> (canonical_sku_id, human_sku_name, rate_usd_per_gb)
EGRESS_SKUS = {
    direction: (v["sku_id"], v["sku_name"], v["rate_usd_per_gb"])
    for direction, v in _egress_cfg.items()
} if _egress_cfg else {
    "interzone":   ("GCP-EGRESS-INTERZONE",   "Network Inter Zone Egress",   0.01),
    "interregion": ("GCP-EGRESS-INTERREGION", "Network Inter Region Egress", 0.08),
    "internet":    ("GCP-EGRESS-INTERNET",    "Network Internet Egress",     0.12),
}

# Cloud CDN Cache Egress — tiered rates per destination bucket.
# AWS CloudFront destination codes (IN, EU, US, …) map to these buckets.
# Tiers are upper-bound GB thresholds; None = unbounded (last tier).
# Source: cloud.google.com/cdn/pricing — verify current rates before finalizing.
_cdn_cfg = {
    k: v for k, v in _cfg("egress-rates").get("cdn_egress", {}).items()
    if not k.startswith("_")
}
CDN_EGRESS_TIERS = _cdn_cfg if _cdn_cfg else {
    "apac": {
        "sku_id": "GCP-CDN-EGRESS-APAC", "sku_name": "Cloud CDN Cache Egress - APAC",
        "tiers": [
            {"up_to_gb": 10240,  "rate_usd_per_gb": 0.08},
            {"up_to_gb": 153600, "rate_usd_per_gb": 0.06},
            {"up_to_gb": None,   "rate_usd_per_gb": 0.05},
        ],
    },
    "americas": {
        "sku_id": "GCP-CDN-EGRESS-AMERICAS", "sku_name": "Cloud CDN Cache Egress - Americas",
        "tiers": [
            {"up_to_gb": 10240,  "rate_usd_per_gb": 0.08},
            {"up_to_gb": 153600, "rate_usd_per_gb": 0.06},
            {"up_to_gb": None,   "rate_usd_per_gb": 0.05},
        ],
    },
    "emea": {
        "sku_id": "GCP-CDN-EGRESS-EMEA", "sku_name": "Cloud CDN Cache Egress - EMEA",
        "tiers": [
            {"up_to_gb": 10240,  "rate_usd_per_gb": 0.08},
            {"up_to_gb": 153600, "rate_usd_per_gb": 0.06},
            {"up_to_gb": None,   "rate_usd_per_gb": 0.05},
        ],
    },
    "apac_au": {
        "sku_id": "GCP-CDN-EGRESS-APAC-AU", "sku_name": "Cloud CDN Cache Egress - Australia",
        "tiers": [
            {"up_to_gb": 10240,  "rate_usd_per_gb": 0.12},
            {"up_to_gb": 153600, "rate_usd_per_gb": 0.09},
            {"up_to_gb": None,   "rate_usd_per_gb": 0.07},
        ],
    },
    "china": {
        "sku_id": "GCP-CDN-EGRESS-CHINA", "sku_name": "Cloud CDN Cache Egress - China",
        "tiers": [
            {"up_to_gb": 512,  "rate_usd_per_gb": 0.20},
            {"up_to_gb": 2048, "rate_usd_per_gb": 0.18},
            {"up_to_gb": None, "rate_usd_per_gb": 0.17},
        ],
    },
}


# GCP legacy regions that charge $0.01/GiBy for inter-zone egress.
# All other regions have been free since October 2023.
_INTERZONE_LEGACY_REGIONS = frozenset({
    "us-central1", "us-east1", "us-west1", "asia-east1", "europe-west1",
})

# Catalog SKU IDs for inter-zone traffic.
_INTERZONE_SKU_PAID = "DE9E-AFBC-A15A"   # 5 legacy regions, $0.01/GiBy
_INTERZONE_SKU_FREE = "C1F1-02CA-F355"   # all other regions, $0.00/GiBy


def interzone_sku_for_region(gcp_region):
    """Return (sku_id, sku_name, rate_usd_per_gb) for inter-zone egress in gcp_region.

    GCP made inter-zone egress free outside the 5 original regions (Oct 2023).
    gcp_region may be None (unknown) — defaults to the paid legacy rate so we
    never under-project when the region is missing.
    """
    if gcp_region and gcp_region not in _INTERZONE_LEGACY_REGIONS:
        return (_INTERZONE_SKU_FREE, "Network Inter Zone Data Transfer Out", 0.0)
    return (_INTERZONE_SKU_PAID, "Network Inter Zone Data Transfer Out", 0.01)


def cdn_egress_rate(bucket, total_gb):
    """Return (sku_id, sku_name, rate_usd_per_gb) for the cheapest applicable tier.

    bucket      — CDN destination bucket key (apac, americas, emea, apac_au, china)
    total_gb    — total monthly GB for this destination (used for tier placement)
    """
    info = CDN_EGRESS_TIERS.get(bucket) or CDN_EGRESS_TIERS.get("apac")
    for tier in info["tiers"]:
        if tier["up_to_gb"] is None or total_gb <= tier["up_to_gb"]:
            return info["sku_id"], info["sku_name"], tier["rate_usd_per_gb"]
    last = info["tiers"][-1]
    return info["sku_id"], info["sku_name"], last["rate_usd_per_gb"]
